"""Command-line entry point for the normalized FAGENT LangGraph workflow."""
import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = 'FAGENT'

import yaml

from .agent.caller import Agent_caller
from .agent.runner import GraphRunner
from .agent.services import GraphServices
from .io.loaders import InputLoader

DEFAULT_CONFIG = Path(__file__).resolve().parent / 'config.json'


def main():
    parser = argparse.ArgumentParser(description='Local GDS DRC agent')
    parser.add_argument('--setup', help='YAML setup for processing a list of cells')
    parser.add_argument('--gds')
    parser.add_argument('--drc', help='Calibre ASCII results database')
    parser.add_argument('--output')
    parser.add_argument('--layer-map', action='append', dest='layer_maps', help='Layer mapping file path (repeatable)')
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    parser.add_argument('--load-only', action='store_true')
    parser.add_argument('--resume-thread', help='Resume a paused graph using its thread ID')
    parser.add_argument('--quiet', action='store_true')

    args = parser.parse_args()
    if args.load_only and args.resume_thread:
        parser.error('--resume-thread cannot be combined with --load-only')
    if args.setup and args.resume_thread:
        parser.error('--resume-thread requires explicit --gds, --drc and --output')

    config = {} if args.load_only else json.loads(Path(args.config).read_text())

    if args.setup:
        if any((args.gds, args.drc, args.output)):
            parser.error('--setup cannot be combined with --gds, --drc or --output')
        setup_path = Path(args.setup).resolve()
        setup = yaml.safe_load(setup_path.read_text())
        if not isinstance(setup, dict):
            parser.error('setup must be a YAML mapping')
        layer_maps = args.layer_maps or setup.get('layer_map_files')
        if not isinstance(layer_maps, list) or not layer_maps or any(not isinstance(p, str) or not p.strip() for p in layer_maps):
            parser.error('layer_map_files must be a non-empty list of paths')
        base = setup_path.parent
        layer_maps = [Path(p).resolve() if args.layer_maps else (base / p).resolve() for p in layer_maps]
        try:
            cell_list = base / setup['list_cells']
            drc_dir = base / setup['drc_folder']
            output = base / setup['output_folder']
            decks = setup['drc_files']
        except KeyError as exc:
            parser.error(f'missing setup field: {exc.args[0]}')
        if isinstance(decks, str):
            decks = [decks]
        config['drc_files'] = [str((base / path).resolve()) for path in decks]
        cells = [line.strip() for line in cell_list.read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        for cell in cells:
            if cell in ('.', '..') or Path(cell).name != cell:
                parser.error(f'invalid cell name: {cell}')
            run_cell(drc_dir / f'{cell}.gds', drc_dir / f'{cell}.drc.results', output / cell, args, config, layer_maps)
    else:
        if not all((args.gds, args.drc, args.output)):
            parser.error('provide --setup or all of --gds, --drc and --output')
        if not args.layer_maps:
            parser.error('--layer-map is required when --setup is not provided')
        layer_maps = [Path(p).resolve() for p in args.layer_maps]
        run_cell(args.gds, args.drc, args.output, args, config, layer_maps)


def run_cell(gds, drc, output, args, config, layer_map_files):
    rundir = Path(output) / 'rundir'
    resume_thread = getattr(args, 'resume_thread', None)
    if not resume_thread:
        loader = InputLoader(gds_path=gds, drc_path=drc, output_dir=rundir,
                             layer_map_files=layer_map_files, verbose=not args.quiet)
        loader.run()
    else:
        for name, source in (('gds.json', gds), ('drc.json', drc)):
            recorded = json.loads((rundir / name).read_text()).get('source')
            if not recorded or Path(recorded).resolve() != Path(source).resolve():
                raise ValueError(f'Resume input does not match {name}')
    if args.load_only:
        result = {'status': 'loaded', 'violations': loader.drc_data['total_violations']}
    else:
        caller = Agent_caller(rundir=rundir, config=config, verbose=not args.quiet)
        services = GraphServices(caller)
        messages = services.prepare()
        if resume_thread:
            runner = GraphRunner(services, rundir, thread_id=resume_thread)
            try:
                feedback = runner.prompt_review()
            except EOFError:
                feedback = None
            result = runner.resume(feedback)
        else:
            result = GraphRunner(services, rundir).run(messages)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()

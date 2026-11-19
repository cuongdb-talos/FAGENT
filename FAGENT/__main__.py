"""Module FAGENT.__main__: Điểm nhập thực thi dòng lệnh (CLI Entry Point) cho hệ thống FAGENT.

Trách nhiệm & Chức năng:
- Xử lý các tham số dòng lệnh CLI (--gds, --drc, --output, --setup, --config, --resume-thread, --load-only).
- Điều phối chế độ chạy hàng loạt theo cấu hình YAML (--setup) hoặc chạy đơn lẻ từng cell (run_cell).
- Nạp dữ liệu GDS/DRC ban đầu thông qua InputLoader, xác thực an toàn không ghi đè dữ liệu cũ.
- Khởi tạo GraphServices và GraphRunner để thực thi quy trình sửa DRC tự động bằng LangGraph.
- Hỗ trợ phục hồi phiên làm việc bị ngắt quãng từ SQLite checkpoint (--resume-thread).
"""
import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = 'FAGENT'

import yaml

from .engine.workflow import load_workflow
from .engine.runner import GraphRunner
from .engine.services import GraphServices
from .capabilities.layout.loaders import InputLoader

DEFAULT_CONFIG = Path(__file__).resolve().parent / 'config.json'


def normalize_goal(raw_prompts):
    if isinstance(raw_prompts, str):
        return raw_prompts.strip()
    if isinstance(raw_prompts, list):
        return '\n'.join(str(item).strip() for item in raw_prompts if str(item).strip())
    return ''


def main():
    parser = argparse.ArgumentParser(description='Local DRC and LVS agent')
    parser.add_argument('--task', choices=('drc', 'lvs'))
    parser.add_argument('--workflow', help='Task definition YAML under an asset root')
    parser.add_argument('--lvs-report', help='Report path to record in the LVS skeleton')
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

    config_path = Path(args.config).resolve()
    config = {} if args.load_only else json.loads(config_path.read_text())
    if config.get('system_prompt_file'):
        config['system_prompt_file'] = str((config_path.parent / config['system_prompt_file']).resolve())
    selected = args.task or config.get('task', 'drc')
    if selected not in ('drc', 'lvs'):
        parser.error('task must be drc or lvs')
    path = args.workflow or config.get('workflow')
    if path and not args.workflow:
        path = config_path.parent / path
    default = 'drc_repair.yaml' if selected == 'drc' else 'lvs.yaml'
    workflow = load_workflow(Path(path) if path else DEFAULT_CONFIG.parent / 'workflows' / default)
    if (args.task or 'task' in config) and selected != workflow.task:
        parser.error('selected task conflicts with workflow')
    args.workflow_definition = workflow
    if workflow.task == 'lvs':
        if args.setup or args.load_only or args.gds or args.drc or args.layer_maps or not args.output:
            parser.error('LVS skeleton requires --output and accepts an optional --lvs-report')
        if args.lvs_report:
            config['lvs_report'] = str(Path(args.lvs_report).resolve())
        result = run_graph(Path(args.output) / 'rundir', args, config)
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.lvs_report:
        parser.error('--lvs-report requires an LVS workflow')

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
        goal = normalize_goal(setup.get('prompts') if 'prompts' in setup else setup.get('prompt'))
        if goal:
            config['goal'] = goal
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
        recorded_maps = json.loads((rundir / 'gds.json').read_text()).get('layer_maps')
        if recorded_maps != [str(Path(path).resolve()) for path in layer_map_files]:
            raise ValueError('Resume layer maps do not match the checkpoint inputs')
    if args.load_only:
        result = {'status': 'loaded', 'violations': loader.drc_data['total_violations']}
        print(json.dumps(result, ensure_ascii=False))
    else:
        run_graph(rundir, args, config)


def run_graph(rundir, args, config):
    services = GraphServices(rundir, config, args.workflow_definition, verbose=not args.quiet)
    if not args.resume_thread and args.workflow_definition.task == 'lvs':
        for name in ('graph-checkpoints.sqlite3', 'conversation.json', 'result.json'):
            artifact = rundir / name
            if artifact.exists() or artifact.is_symlink():
                raise ValueError(f'Output already exists: {artifact}')
    if args.resume_thread and not (rundir / 'graph-checkpoints.sqlite3').is_file():
        raise ValueError('No checkpoint database in the requested rundir')
    messages = services.prepare(resume=bool(args.resume_thread))
    runner = GraphRunner(services, rundir, thread_id=args.resume_thread)
    if args.resume_thread:
        return runner.resume()
    return runner.run(messages)


if __name__ == '__main__':
    main()

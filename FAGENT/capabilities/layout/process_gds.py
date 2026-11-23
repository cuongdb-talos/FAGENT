"""Low-level binary GDSII parser and layer-map resolver."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
import copy
import json
from pathlib import Path
import re
import struct

import numpy


# ==============================================================================
# SECTION 2: GDSII Binary Stream Parser and Geometry Processor Class
# ==============================================================================
class ProcessGDS:
    """Decodes binary GDSII records, applies layer mapping, and builds cell hierarchy geometry."""

    # --------------------------------------------------------------------------
    # Main Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self, gds_file=None, layer_map_files=None):
        if gds_file is not None:
            self.gds_file = gds_file
        if layer_map_files is not None:
            self.layer_map_files = layer_map_files
        self.param_dict = {}
        self.read_layer_mapping()
        self.read_gds()
        return self

    def __init__(self, gds_file=None, layer_map_files=None):
        self.gds_file = gds_file
        self.layer_map_files = layer_map_files
        self.param_dict = {}
        self.gds_db = {}

    # --- GDSII stream decoder: extract structs, polygons, paths, texts, and references ---
    def read_gds(self):

        strans = 0
        angle = 0
        mag = 0
        path_type = 0
        bgnextn = None
        endextn = None
        with open(self.gds_file, 'rb') as infile:
            for record in self.record_reader(infile):
                if record[0] == 0x01:
                    self.gds_db = {}
                elif record[0] == 0x02:
                    self.gds_db["name"] = record[1]
                elif record[0] == 0x03:
                    self.gds_db["unit"] = [float(num) for num in list(record[1])]
                # BGNSTR
                elif record[0] == 0x06:
                    if "structs" not in self.gds_db:
                        self.gds_db["structs"] = {}
                    struct_name = record[1]
                    self.gds_db["structs"][struct_name] = {
                        "width": 0,
                        "height": 0,
                        "coord": [],
                        "poly": [],
                        "path": [],
                        "text": [],
                        "sref": [],
                        "aref": [],
                    }
                # BOUNDRY
                elif record[0] == 0x08:
                    ele_type = "poly"
                elif record[0] == 0x0D:
                    layer = int(list(record[1])[0])
                elif record[0] == 0x0E:
                    data_type = int(list(record[1])[0])
                elif record[0] == 0x10:
                    coord = [int(num) for num in list(record[1])]
                # PATH
                elif record[0] == 0x09:
                    ele_type = "path"
                elif record[0] == 0x0F:
                    width = int(list(record[1])[0])
                elif record[0] == 0x21:
                    path_type = int(list(record[1])[0])
                elif record[0] == 0x30:
                    bgnextn = int(list(record[1])[0])
                elif record[0] == 0x31:
                    endextn = int(list(record[1])[0])
                # TEXT
                elif record[0] == 0x0C:
                    ele_type = "text"
                elif record[0] == 0x16:
                    text_type = int(list(record[1])[0])
                elif record[0] == 0x17:
                    text_presentation = int(list(record[1])[0])
                elif record[0] == 0x19:
                    text_content = record[1]
                # SREF
                elif record[0] == 0x0A:
                    ele_type = "sref"
                elif record[0] == 0x12:
                    sname = record[1]
                elif record[0] == 0x1C:
                    angle = float(list(record[1])[0])
                elif record[0] == 0x1A:
                    strans = int(list(record[1])[0])
                elif record[0] == 0x1B:
                    mag = float(list(record[1])[0])
                # AREF 
                elif record[0] == 0x0B:
                    ele_type = "aref"
                elif record[0] == 0x13:
                    col_row = [int(num) for num in list(record[1])]
                # END ELEMENT
                elif record[0] == 0x11:
                    if ele_type == "poly":
                        if (layer, data_type) in self.layer_mapping:
                            if self.layer_mapping[(layer, data_type)][0] == "BOUNDARY":
                                list_x = sorted([coord[i] for i in range(0, len(coord), 2)])
                                list_y = sorted([coord[i] for i in range(1, len(coord), 2)])
                                min_x = list_x[0]
                                max_x = list_x[-1]
                                min_y = list_y[0]
                                max_y = list_y[-1]
                                if (
                                    max_x - min_x >= self.gds_db["structs"][struct_name]["width"] and
                                    max_y - min_y >= self.gds_db["structs"][struct_name]["height"]
                                ):
                                    self.gds_db["structs"][struct_name]["width"] = max_x - min_x
                                    self.gds_db["structs"][struct_name]["height"] = max_y - min_y
                                    self.gds_db["structs"][struct_name]["coord"] = [min_x, min_y]
                                    self.gds_db["structs"][struct_name]["x"] = [min_x, max_x]
                                    self.gds_db["structs"][struct_name]["y"] = [min_y, max_y]
                                
                            self.gds_db["structs"][struct_name]["poly"].append(
                                {
                                    "kind": "poly",
                                    "layer": layer,
                                    "data_type": data_type,
                                    "coord": coord,
                                }
                            )
                        else:
                            self.gds_db["structs"][struct_name]["poly"].append(
                                {
                                    "kind": "poly",
                                    "layer": layer,
                                    "data_type": data_type,
                                    "coord": coord,
                                }
                            )
                    elif ele_type == "path":
                        self.gds_db["structs"][struct_name]["poly"].append(
                            {
                                "kind": "path",
                                "editable": False,
                                "layer": layer,
                                "data_type": data_type,
                                "coord": self.convert_path_to_poly(coord, width, path_type, bgnextn, endextn)
                            }
                        )
                    
                        self.gds_db["structs"][struct_name]["path"].append(
                            {
                                "layer": layer,
                                "data_type": data_type,
                                "path_type": path_type,
                                "bgnextn": bgnextn,
                                "endextn": endextn,
                                "width": width,
                                "coord": coord,
                            }
                        )
                    elif ele_type == "text":
                        text_db = {
                            "layer": layer,
                            "text_type": text_type,
                            "text_presentation": text_presentation,
                            "coord": coord,
                            "text_content": text_content,
                            "angle": angle,
                            "mag": mag,
                            "is_flip": 1 if strans else 0,
                        }
                        self.gds_db["structs"][struct_name]["text"].append(text_db)
                    elif ele_type == "sref":
                        self.gds_db["structs"][struct_name]["sref"].append({
                            "sname": sname,
                            "coord": coord,
                            "is_flip": 1 if strans else 0,
                            "angle": angle,
                        })
                    elif ele_type == "aref":
                        self.gds_db["structs"][struct_name]["aref"].append(
                            {
                                "sname": sname,
                                "col_row": col_row,
                                "coord": coord,
                                "is_flip": 1 if strans else 0,
                                "angle": angle,
                            }
                        )
                    ele_type = ""
                    strans = 0
                    angle = 0
                    mag = 0
                    path_type = 0
                    bgnextn = None
                    endextn = None
                elif record[0] == 0x07:
                    # Get poly of pin
                    self.is_flip_and_angle = [[0,0]]
                    self.coord = [[0,0]]
                    self.get_poly_for_pin(root_cell = struct_name, 
                                          ref_cell = struct_name, 
                                          list_text_index = [i for i in range(len(self.gds_db["structs"][struct_name]["text"]))], 
                                          )
                    self.is_flip_and_angle = []
                    self.coord  = []
                    if self.gds_db["structs"][struct_name]["width"] == 0 or self.gds_db["structs"][struct_name]["height"] == 0:
                        boundary = self.get_boundary_for_cell(struct_name)
                        self.gds_db["structs"][struct_name]["boundary"] = boundary
                        min_x, min_y = boundary[0], boundary[1]
                        max_x, max_y = boundary[4], boundary[5]
                        if self.gds_db["structs"][struct_name]["width"] == 0:
                            self.gds_db["structs"][struct_name]["width"] = max_x - min_x
                            self.gds_db["structs"][struct_name]["x"] = [min_x, max_x]
                        if self.gds_db["structs"][struct_name]["height"] == 0:
                            self.gds_db["structs"][struct_name]["height"] = max_y - min_y
                            self.gds_db["structs"][struct_name]["y"] = [min_y, max_y]
                        if not self.gds_db["structs"][struct_name]["coord"]:
                            self.gds_db["structs"][struct_name]["coord"] = [min_x, min_y]

    # --- Cell boundary computation: compute overall bounding box from polys, paths, and srefs ---
    def get_boundary_for_cell(self, cell_name):
        min_x = None
        min_y = None
        max_x = None
        max_y = None

        cell_struct = self.gds_db["structs"][cell_name]

        # 1. Inspect direct polygons
        for poly_db in cell_struct.get("poly", []):
            coord = poly_db["coord"]
            list_x = [coord[i] for i in range(0, len(coord), 2)]
            list_y = [coord[i] for i in range(1, len(coord), 2)]
            if list_x and list_y:
                p_min_x, p_max_x = min(list_x), max(list_x)
                p_min_y, p_max_y = min(list_y), max(list_y)
                min_x = p_min_x if min_x is None else min(min_x, p_min_x)
                max_x = p_max_x if max_x is None else max(max_x, p_max_x)
                min_y = p_min_y if min_y is None else min(min_y, p_min_y)
                max_y = p_max_y if max_y is None else max(max_y, p_max_y)

        # 2. Inspect paths
        for path_db in cell_struct.get("path", []):
            coord = path_db["coord"]
            list_x = [coord[i] for i in range(0, len(coord), 2)]
            list_y = [coord[i] for i in range(1, len(coord), 2)]
            if list_x and list_y:
                p_min_x, p_max_x = min(list_x), max(list_x)
                p_min_y, p_max_y = min(list_y), max(list_y)
                min_x = p_min_x if min_x is None else min(min_x, p_min_x)
                max_x = p_max_x if max_x is None else max(max_x, p_max_x)
                min_y = p_min_y if min_y is None else min(min_y, p_min_y)
                max_y = p_max_y if max_y is None else max(max_y, p_max_y)

        # 3. Inspect srefs for hierarchical boundary
        for sref_db in cell_struct.get("sref", []):
            child_name = sref_db["sname"]
            if child_name in self.gds_db["structs"]:
                child_struct = self.gds_db["structs"][child_name]
                child_w = child_struct.get("width", 0)
                child_h = child_struct.get("height", 0)
                c_x = sref_db["coord"][0] if len(sref_db["coord"]) > 0 else 0
                c_y = sref_db["coord"][1] if len(sref_db["coord"]) > 1 else 0
                if child_w > 0 and child_h > 0:
                    min_x = c_x if min_x is None else min(min_x, c_x)
                    min_y = c_y if min_y is None else min(min_y, c_y)
                    max_x = c_x + child_w if max_x is None else max(max_x, c_x + child_w)
                    max_y = c_y + child_h if max_y is None else max(max_y, c_y + child_h)

        if min_x is None:
            min_x, min_y, max_x, max_y = 0, 0, 0, 0

        return [
            min_x,
            min_y,
            max_x,
            min_y,
            max_x,
            max_y,
            min_x,
            max_y,
            min_x,
            min_y
        ]

    # --- Pin-to-polygon association: match text labels to underlying metal polygons recursively ---
    def get_poly_for_pin(self, root_cell, ref_cell, list_text_index):
        ref_cell_db = self.gds_db["structs"][ref_cell]
        root_cell_db = self.gds_db["structs"][root_cell]
        poly_hash = {}

        for index, poly_db in enumerate(ref_cell_db["poly"]):
            layer = poly_db["layer"]
            data_type = poly_db["data_type"]
            
            if (layer, data_type) in self.layer_mapping:
                if self.layer_mapping[(layer, data_type)][0] not in poly_hash:
                    poly_hash[self.layer_mapping[(layer, data_type)][0]] = []
                poly_hash[self.layer_mapping[(layer, data_type)][0]].append(index)

        for index in list_text_index:
            text_db = root_cell_db["text"][index]
            layer = text_db["layer"]
            text_type = text_db["text_type"]
            if (layer, text_type) in self.layer_mapping:
                if self.layer_mapping[(layer, text_type)][0] in poly_hash:
                    for poly_index in poly_hash[self.layer_mapping[(layer, text_type)][0]]:
                        new_poly = ref_cell_db["poly"][poly_index]["coord"]
                        for i in range(len(self.is_flip_and_angle)-1, -1, -1):
                            new_poly = [new_poly[p_id] + self.coord[i][0] if p_id % 2 == 0 else new_poly[p_id] + self.coord[i][1] for p_id in range(len(new_poly))]
                            is_flip = self.is_flip_and_angle[i][0]
                            angle = self.is_flip_and_angle[i][1]
                            if is_flip:
                                new_poly = self.flip_poly(new_poly, self.coord[i])
                            if angle:
                                new_poly = self.rotate_poly(angle, new_poly, self.coord[i])
                        
                        if self.is_inside_poly(
                            point = text_db["coord"],
                            poly = new_poly
                        ):  
                            self.gds_db["structs"][root_cell]["text"][index]["poly"] = new_poly
                        
            if "poly" not in self.gds_db["structs"][root_cell]["text"][index]:
                for sref in self.gds_db["structs"][ref_cell]["sref"]:
                    temp_sref_coord = copy.deepcopy(sref["coord"])
                    if "is_flip" in sref and sref["is_flip"]:
                        is_flip = 1
                    else:
                        is_flip = 0
                    if "angle" in sref:
                        angle = int(sref["angle"])
                    else:
                        angle = 0
                    self.is_flip_and_angle.append([is_flip, angle])
                    self.coord.append(temp_sref_coord)
                    self.get_poly_for_pin(root_cell, sref["sname"], [index])
                    self.is_flip_and_angle = self.is_flip_and_angle[:-1]
                    self.coord = self.coord[:-1]
        return

    # --- Path expansion: convert center-line GDS paths into closed boundary polygons ---
    def convert_path_to_poly(self, coord, width, path_type=0, bgnextn=None, endextn=None):
        if len(coord) < 4:
            return coord
        
        pts = []
        for i in range(0, len(coord), 2):
            p = (coord[i], coord[i+1])
            if not pts or pts[-1] != p:
                pts.append(p)
                
        if len(pts) < 2:
            return coord

        w = width // 2
        
        ext_bg = 0
        ext_end = 0
        if path_type == 2:
            ext_bg = w
            ext_end = w
        elif path_type == 4:
            ext_bg = bgnextn if bgnextn is not None else 0
            ext_end = endextn if endextn is not None else 0

        left_trace = []
        right_trace = []

        for i in range(len(pts)):
            if i == 0:
                dx, dy = self._get_dir(pts[0], pts[1])
                nx, ny = -dy, dx
                px = pts[0][0] - dx * ext_bg
                py = pts[0][1] - dy * ext_bg
                left_trace.append((px + nx * w, py + ny * w))
                right_trace.append((px - nx * w, py - ny * w))
            elif i == len(pts) - 1:
                dx, dy = self._get_dir(pts[-2], pts[-1])
                nx, ny = -dy, dx
                px = pts[-1][0] + dx * ext_end
                py = pts[-1][1] + dy * ext_end
                left_trace.append((px + nx * w, py + ny * w))
                right_trace.append((px - nx * w, py - ny * w))
            else:
                dx_in, dy_in = self._get_dir(pts[i-1], pts[i])
                nx_in, ny_in = -dy_in, dx_in
                
                dx_out, dy_out = self._get_dir(pts[i], pts[i+1])
                nx_out, ny_out = -dy_out, dx_out
                
                if abs(dx_in * dy_out - dx_out * dy_in) < 1e-6:
                    left_trace.append((pts[i][0] + nx_in * w, pts[i][1] + ny_in * w))
                    right_trace.append((pts[i][0] - nx_in * w, pts[i][1] - ny_in * w))
                else:
                    left_trace.append((pts[i][0] + (nx_in + nx_out) * w, pts[i][1] + (ny_in + ny_out) * w))
                    right_trace.append((pts[i][0] - (nx_in + nx_out) * w, pts[i][1] - (ny_in + ny_out) * w))

        poly = []
        for p in left_trace:
            poly.extend([int(round(p[0])), int(round(p[1]))])
        for p in reversed(right_trace):
            poly.extend([int(round(p[0])), int(round(p[1]))])
            
        poly.extend([int(round(left_trace[0][0])), int(round(left_trace[0][1]))])
        
        return poly

    def _get_dir(p0, p1):
        dx = p1[0] - p0[0]
        dy = p1[1] - p0[1]
        length = (dx**2 + dy**2)**0.5
        if length == 0:
            return 0.0, 0.0
        return dx / length, dy / length

    _get_dir = staticmethod(_get_dir)

    # --- Layer map loader: map (layer, datatype) pairs to layer names and purposes ---
    def read_layer_mapping(self):
        self.layer_mapping = {}
        if not self.layer_map_files:
            raise ValueError('At least one layer mapping file must be provided')
        for path in self.layer_map_files:
            with open(path, 'r') as stream:
                for line in stream:
                    if line.lstrip().startswith('#') or not line.strip():
                        continue
                    fields = line.split()
                    self.layer_mapping[(int(fields[0]), int(fields[1]))] = [fields[2], fields[3]]

    # --- Geometric transforms: mirror and rotate polygon coordinates around an origin ---
    def flip_poly(self, poly, coord):
        for i in range(len(poly)):
            if i % 2 == 1:
                poly[i] = -poly[i] + 2 * coord[1]
        return poly
    
    def rotate_poly(self, angle, poly, coord):
        poly = [poly[i] - coord[0] if i % 2 == 0 else poly[i] - coord[1] for i in range(len(poly))]
        if angle == 90:
            for i in range(len(poly)):
                if i % 2 == 0:
                    temp_x = poly[i]
                    poly[i] = -poly[i + 1]
                else:
                    poly[i] = temp_x
        elif angle == 180:
            for i in range(len(poly)):
                poly[i] = -poly[i]
        elif angle == 270:
            for i in range(len(poly)):
                if i % 2 == 0:
                    temp_x = poly[i]
                    poly[i] = poly[i + 1]
                else:
                    poly[i] = -temp_x
        poly = [poly[i] + coord[0] if i % 2 == 0 else poly[i] + coord[1] for i in range(len(poly))]
        return poly

    # --- Binary record & real-number utilities: decode/encode GDSII records and 8-byte reals ---
    def record_reader(self, stream):
        """
        Generator for complete records from a GDSII stream file.

        Parameters
        ----------
        stream : file
            GDSII stream file to be read.

        Returns
        -------
        out : list
            Record type and data (as a numpy.array, string or None)
        """
        while True:
            header = stream.read(4)
            if len(header) < 4:
                return
            size, rec_type = struct.unpack(">HH", header)
            data_type = rec_type & 0x00FF
            rec_type = rec_type // 256
            data = None
            content = None
            if size > 4:
                if data_type == 0x01:
                    content = stream.read(size - 4)
                    data = numpy.array(
                        struct.unpack(
                            ">{0}H".format((size - 4) // 2), content
                        ),
                        dtype="uint",
                    )
                elif data_type == 0x02:
                    content = stream.read(size - 4)
                    data = numpy.array(
                        struct.unpack(
                            ">{0}h".format((size - 4) // 2), content
                        ),
                        dtype="int",
                    )
                elif data_type == 0x03:
                    content = stream.read(size - 4)
                    data = numpy.array(
                        struct.unpack(
                            ">{0}l".format((size - 4) // 4), content
                        ),
                        dtype="int",
                    )
                elif data_type == 0x05:
                    # content = stream.read(8)
                    data = numpy.array(
                        [
                            self.eight_byte_real_to_float(stream.read(8))
                            for _ in range((size - 4) // 8)
                        ]
                    )
                else:
                    content = stream.read(size - 4)
                    data = content
                    if str is not bytes:
                        if data[-1] == 0:
                            data = data[:-1].decode("ascii")
                        else:
                            data = data.decode("ascii")
                    elif data[-1] == "\0":
                        data = data[:-1]
            yield [rec_type, data]

    def eight_byte_real_to_float(self, value):

        short1, short2, long3 = struct.unpack(">HHL", value)
        exponent = (short1 & 0x7F00) // 256 - 64
        mantissa = (
            ((short1 & 0x00FF) * 65536 + short2) * 4294967296 + long3
        ) / 72057594037927936.0
        if short1 & 0x8000:
            return -mantissa * 16.0 ** exponent
        return mantissa * 16.0 ** exponent
    
    # --- Parameter & containment helpers: substitute template params and bounding-box point check ---
    def replace_param(self, line, param_dict):
        for param in sorted(re.findall(r"(\$[a-zA-Z0-9_]+)", line), reverse = True):
            if f"{param}_in_func" in param_dict:
                if f"{{{param}}}" in line:
                    line = line.replace(f"{{{param}}}", str(param_dict[f"{param}_in_func"]))
                else:
                    line = line.replace(param, str(param_dict[f"{param}_in_func"]))
            elif param in param_dict:
                if f"{{{param}}}" in line:
                    line = line.replace(f"{{{param}}}", str(param_dict[param]))
                else:
                    line = line.replace(param, str(param_dict[param]))
        return line

    def is_inside_poly(self, point, poly):
        xmin = min([poly[i] for i in range(0, len(poly), 2)])
        xmax = max([poly[i] for i in range(0, len(poly), 2)])
        ymin = min([poly[i] for i in range(1, len(poly), 2)])
        ymax = max([poly[i] for i in range(1, len(poly), 2)])
        if (
            point[0] >= xmin and
            point[0] <= xmax and
            point[1] >= ymin and
            point[1] <= ymax
        ):
            return True

        return False

    def eight_byte_real(self, value):
        """
        Convert a number into the GDSII 8 byte real format.

        Parameters
        ----------
        value : number
            The number to be converted.

        Returns
        -------
        out : string
            The GDSII binary string that represents `value`.
        """
        if value == 0:
            return b"\x00\x00\x00\x00\x00\x00\x00\x00"
        if value < 0:
            byte1 = 0x80
            value = -value
        else:
            byte1 = 0x00
        fexp = numpy.log2(value) / 4
        exponent = int(numpy.ceil(fexp))
        if fexp == exponent:
            exponent += 1
        mantissa = int(value * 16.0 ** (14 - exponent))
        byte1 += exponent + 64
        byte2 = mantissa // 281474976710656
        short3 = (mantissa % 281474976710656) // 4294967296
        long4 = mantissa % 4294967296
        return struct.pack(">HHL", byte1 * 256 + byte2, short3, long4)


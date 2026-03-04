import glob
import re
import os
import cv2
import numpy as np
import svgpathtools

def get_svg_size(content):
    vw, vh = 1000, 1000
    w_m = re.search(r'viewBox="0 0 (\d+) (\d+)"', content)
    if w_m:
        vw, vh = int(w_m.group(1)), int(w_m.group(2))
    else:
        width_m = re.search(r'width="([\d.]+)"', content)
        height_m = re.search(r'height="([\d.]+)"', content)
        if width_m: vw = int(float(width_m.group(1)))
        if height_m: vh = int(float(height_m.group(1)))
    return vw, vh

def main():
    ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))
    # Use the moved input_svgs inside the package if available, else fallback
    PKG_INPUT_DIR = os.path.join(os.path.dirname(__file__), "input_svgs")
    INPUT_DIR = PKG_INPUT_DIR if os.path.exists(PKG_INPUT_DIR) else os.path.join(ROOT_DIR, "data", "scraps", "input_svgs")
    OUTPUT_DIR = os.path.join(ROOT_DIR, "data", "assets", "plans")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    svgs = glob.glob(os.path.join(INPUT_DIR, "*.svg"))
    
    if not svgs:
        print(f"No SVGs found in {INPUT_DIR}")
        return

    for f in svgs:
        basename = os.path.basename(f)
        print(f"Processing {basename}...")
        
        with open(f, 'r') as file:
            content = file.read()
            
        w, h = get_svg_size(content)
        
        # Rasterize walls
        paths = re.findall(r'<path[^>]*d="([^"]+)"', content)
        scale = 2.0
        img = np.zeros((int(h*scale)+10, int(w*scale)+10), dtype=np.uint8)
        
        # Let's draw walls with robust thickness to close gaps
        thickness = 6
        for d in paths:
            try:
                path = svgpathtools.parse_path(d)
                for seg in path:
                    if type(seg) == svgpathtools.path.Line:
                        p1 = (int(seg.start.real * scale), int(seg.start.imag * scale))
                        p2 = (int(seg.end.real * scale), int(seg.end.imag * scale))
                        cv2.line(img, p1, p2, 255, thickness)
                    else:
                        num_points = max(2, int(seg.length() * scale / 2))
                        pts = [seg.point(t) for t in np.linspace(0, 1, num_points)]
                        cv_pts = np.array([[[int(p.real * scale), int(p.imag * scale)]] for p in pts], dtype=np.int32)
                        cv2.polylines(img, [cv_pts], False, 255, thickness=thickness)
            except Exception as e:
                pass

        # Use re.sub to inject <a> tags
        def process_match(m):
            full_text_tag = m.group(0)
            room_id = m.group(1)
            if not room_id.isdigit():
                return full_text_tag
                
            x_match = re.search(r'x="([-\d.]+)"', full_text_tag)
            y_match = re.search(r'y="([-\d.]+)"', full_text_tag)
            if not x_match or not y_match:
                return full_text_tag
                
            x = float(x_match.group(1))
            y = float(y_match.group(1))
            
            # Adjust Y upwards slightly because SVG y is baseline
            cx = int(x * scale)
            cy = int((y - 4) * scale) # baseline is 12px font, center is ~4px up
            
            # Flood Fill
            mask = np.zeros((img.shape[0]+2, img.shape[1]+2), dtype=np.uint8)
            # Ensure points are inside bounds
            cx = max(0, min(cx, img.shape[1]-1))
            cy = max(0, min(cy, img.shape[0]-1))
            
            # If the seed point happens to be on a wall, we try to nudge it
            nudge_directions = [(0,0), (0,-5), (0,5), (5,0), (-5,0), (5,-5), (-5,5), (5,5), (-5,-5)]
            filled = False
            for dx, dy in nudge_directions:
                tcx, tcy = cx + dx, cy + dy
                if 0 <= tcx < img.shape[1] and 0 <= tcy < img.shape[0] and img[tcy, tcx] == 0:
                    cv2.floodFill(img, mask, (tcx, tcy), 128)
                    area = np.sum(mask == 1)
                    # If area is too big (leaked out to whole map), or too small, skip
                    if area < 500 or area > (img.shape[0] * img.shape[1] * 0.5):
                        # undo
                        img[img == 128] = 0
                        mask[:] = 0
                        continue
                    filled = True
                    break
                    
            if filled:
                # find contours of the mask
                contours, _ = cv2.findContours(mask[1:-1, 1:-1], cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                if contours:
                    c = max(contours, key=cv2.contourArea)
                    # approx poly
                    peri = cv2.arcLength(c, True)
                    approx = cv2.approxPolyDP(c, 0.01 * peri, True) # 1% error
                    
                    # convert to SVG points
                    points_str = " ".join([f"{pt[0][0]/scale:.1f},{pt[0][1]/scale:.1f}" for pt in approx])
                    polygon = f'<polygon class="room-area transition-all duration-300" points="{points_str}" fill="transparent" />'
                    
                    modified_text = re.sub(r'<text ', r'<text class="room-label" pointer-events="none" fill="white" ', full_text_tag)
                    
                    # clean up img for next fill
                    img[img == 128] = 0
                    
                    return f'<a data-room="{room_id}" class="cursor-pointer group">\n{polygon}\n{modified_text}\n</a>'
            
            # Fallback rect
            modified_text = re.sub(r'<text ', r'<text class="room-label" pointer-events="none" fill="white" ', full_text_tag)
            rect = f'<rect class="room-area transition-all duration-300" x="{x-15}" y="{y-25}" width="60" height="40" rx="6" fill="transparent" />'
            img[img == 128] = 0
            return f'<a data-room="{room_id}" class="cursor-pointer group">\n{rect}\n{modified_text}\n</a>'

        new_content = re.sub(r'<text id="([^"]+)"[^>]*>.*?</text>', process_match, content, flags=re.DOTALL)
        
        # The svg should have transparent background and white walls
        # Remove white rect background
        new_content = re.sub(r'<rect[^>]*fill="white"[^>]*/>', '', new_content)
        # Turn all strokes to white with uniform thickness
        new_content = re.sub(r'stroke="[^"]+"', r'stroke="white"', new_content)
        new_content = re.sub(r'stroke-width="[^"]+"', r'stroke-width="2"', new_content)
        
        out_path = os.path.join(OUTPUT_DIR, basename)
        with open(out_path, 'w') as file:
            file.write(new_content)

    print("Processing complete.")

if __name__ == "__main__":
    main()

"""RBPMS+ soma candidate counting, red/green auto channel. Research prototype; validate overlays.
Windows: .\\.venv\\Scripts\\python.exe rbpms_counter_recursive.py --input "D:\\data" --output "D:\\results" --channel green --show-ids
Requires numpy, scipy, scikit-image, opencv-python.
"""
from __future__ import annotations
import argparse, csv, re
from pathlib import Path
import cv2
import numpy as np
from scipy import ndimage as ndi
from skimage.feature import peak_local_max
from skimage.morphology import remove_small_objects
from skimage.segmentation import watershed, find_boundaries

DEFAULTS = {'red': {'floor': 65, 'min_distance': 11},
            'green': {'floor': 55, 'min_distance': 8}}

def load_png(path):
    raw = cv2.imdecode(np.fromfile(str(path),dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if raw is None: raise ValueError('Cannot read image')
    if raw.ndim != 3 or raw.shape[2] < 3: raise ValueError('Expected 8-bit RGB/RGBA PNG')
    if raw.dtype != np.uint8: raise ValueError('Expected 8-bit PNG; convert with retained intensity metadata')
    return cv2.cvtColor(raw[:,:,:3],cv2.COLOR_BGR2RGB)

def select_channel(rgb, requested):
    if requested in ('red','green'):return requested
    # The original dataset has only a green or red signal, not two meaningful markers.
    # If both channels are materially bright, require the user to specify the marker.
    r,g=rgb[:,:,0],rgb[:,:,1]
    rs=float(np.percentile(r,99)); gs=float(np.percentile(g,99))
    if rs>40 and gs>40 and 0.4<rs/gs<2.5:
        raise ValueError('Both red and green are strong; set --channel red or green explicitly')
    return 'red' if rs>gs else 'green'

def segment(signal, *, floor, min_distance, min_area=40, max_area=1600,
            local_contrast=9, min_peak_distance=2.8, blur=1.1,
            background_sigma=18):
    sm=cv2.GaussianBlur(signal,(0,0),blur)
    bg=cv2.GaussianBlur(sm,(0,0),background_sigma)
    fg=(sm>=floor)&((sm.astype(np.float32)-bg)>=local_contrast)
    fg=remove_small_objects(fg,max_size=13)
    fg=ndi.binary_fill_holes(fg)
    fg=remove_small_objects(fg,max_size=13)
    dist=ndi.gaussian_filter(ndi.distance_transform_edt(fg),sigma=0.8)
    pts=peak_local_max(dist,min_distance=min_distance,threshold_abs=min_peak_distance,
                       labels=fg.astype(np.uint8),exclude_border=False)
    seeds=np.zeros(signal.shape,dtype=bool)
    if len(pts): seeds[pts[:,0],pts[:,1]]=True
    markers,_=ndi.label(seeds)
    raw_labels=watershed(-dist,markers=markers,mask=fg)
    area=np.bincount(raw_labels.ravel())
    chosen=np.flatnonzero((area>=min_area)&(area<=max_area)&(np.arange(len(area))!=0))
    lut=np.zeros(len(area),dtype=np.int32)
    lut[chosen]=np.arange(1,len(chosen)+1)
    return lut[raw_labels]

def savepng(path, rgb_or_gray):
    arr=rgb_or_gray
    if arr.ndim==3: arr=cv2.cvtColor(arr,cv2.COLOR_RGB2BGR)
    ok,buf=cv2.imencode('.png',arr)
    if not ok:raise OSError('PNG encoding failed: '+str(path))
    buf.tofile(str(path))

def analyze(path, out, args):
    rgb=load_png(path)
    channel=select_channel(rgb,args.channel)
    config=DEFAULTS[channel]
    floor=config['floor'] if args.floor is None else args.floor
    md=config['min_distance'] if args.min_distance is None else args.min_distance
    signal=rgb[:,:,0 if channel=='red' else 1]
    labels=segment(signal,floor=floor,min_distance=md,min_area=args.min_area,
                   local_contrast=args.local_contrast)
    n=int(labels.max())
    overlay=rgb.copy()
    overlay[find_boundaries(labels,mode='outer')]=[255,255,0] # yellow outlines both red/green
    rows=[]
    for cell_id,sl in enumerate(ndi.find_objects(labels),1):
        if sl is None:continue
        part=labels[sl]==cell_id
        yy,xx=np.nonzero(part)
        x=float(xx.mean()+sl[1].start);y=float(yy.mean()+sl[0].start)
        rr=rgb[sl][:,:,0][part]; gg=rgb[sl][:,:,1][part]
        rows.append({'id':cell_id,'x_px':round(x,2),'y_px':round(y,2),
                     'area_px2':int(part.sum()),'mean_marker':round(float(signal[sl][part].mean()),2),
                     'mean_red':round(float(rr.mean()),2),'mean_green':round(float(gg.mean()),2)})
        xi,yi=int(round(x)),int(round(y))
        cv2.circle(overlay,(xi,yi),2,(255,0,255),-1)
        if args.show_ids:
            cv2.putText(overlay,str(cell_id),(xi+3,yi-3),cv2.FONT_HERSHEY_SIMPLEX,.27,(255,255,255),1,cv2.LINE_AA)
    out.mkdir(parents=True,exist_ok=True)
    savepng(out/(path.stem+'_overlay.png'),overlay)
    if n<=65535:savepng(out/(path.stem+'_labels16.png'),labels.astype(np.uint16))
    with (out/(path.stem+'_cells.csv')).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['id','x_px','y_px','area_px2','mean_marker','mean_red','mean_green'])
        w.writeheader();w.writerows(rows)
    return {'image':path.name,'channel':channel,'count':len(rows),'floor':floor,'min_distance':md,
            'status':'UNVERIFIED; compare overlay with manual annotations'}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',required=True,help='PNG file or folder')
    p.add_argument('--output',default='rbpms_v2_results')
    p.add_argument('--channel',choices=['auto','red','green'],default='auto')
    p.add_argument('--floor',type=float,default=None,help='Override signal floor (0-255)')
    p.add_argument('--min-distance',type=int,default=None,help='Override minimum distance between centers (px)')
    p.add_argument('--min-area',type=int,default=40)
    p.add_argument('--local-contrast',type=float,default=9)
    p.add_argument('--show-ids',action='store_true')
    p.add_argument('--suffix',default='_c1',help='Only count PNGs whose filename stem ends with this suffix (default: _c1)')
    args=p.parse_args()
    source=Path(args.input);out=Path(args.output)
    if not source.exists():p.error('Input not found')
    # Recursively discover only the selected source channel, not the merged or DAPI images.
    candidates=sorted(source.rglob('*.png'),key=lambda x:str(x).casefold()) if source.is_dir() else [source]
    paths=[x for x in candidates if x.stem.casefold().endswith(args.suffix.casefold())]
    if not paths:p.error(f'No PNG files ending in {args.suffix!r} were found under the input path')
    report=[]
    for path in paths:
        try:
            relative=path.relative_to(source) if source.is_dir() else Path(path.name)
            # Mirror the experiment folder structure to prevent equal filename collisions.
            target=out/relative.parent
            result=analyze(path,target,args)
            result['relative_path']=str(relative)
            result['folder']=str(relative.parent)
            report.append(result)
            print(f"{relative}: {result['count']} [{result['channel']}]",flush=True)
        except Exception as ex:
            relative=path.relative_to(source) if source.is_dir() else Path(path.name)
            print('ERROR',relative,ex,flush=True)
            report.append({'relative_path':str(relative),'folder':str(relative.parent),
                           'image':path.name,'channel':'','count':'','floor':'',
                           'min_distance':'','status':'ERROR: '+str(ex)})
    out.mkdir(parents=True,exist_ok=True)
    with (out/'summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['relative_path','folder','image','channel','count','floor','min_distance','status'])
        w.writeheader();w.writerows(report)
    print(f'Processed {len(paths)} matching image(s). Summary: {out/"summary.csv"}')

if __name__=='__main__':main()

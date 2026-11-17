"""RBPMS V3: conservative strong-cell counts plus weak-candidate review.

This is an image-review prototype, NOT an autonomous validated cell counter.
It never infers vessels reliably from green RBPMS alone. Optional manual masks
apply to one image only. Green-only PNG files can be searched recursively.

Windows single-image example:
  .\\.venv\\Scripts\\python.exe rbpms_counter_v3.py --input "D:\\test_c1.png" --output "D:\\v3_test" --vessel-mask "D:\\vessel_mask.png"
Recursive images:
  .\\.venv\\Scripts\\python.exe rbpms_counter_v3.py --input "D:\\data" --output "D:\\v3_results"

Color coding in overlay: cyan=strong candidate, yellow=weak candidate for
manual review, magenta=rejected candidate. The latter two should not be
silently added to or excluded from a publication-quality cell count.
"""
from __future__ import annotations
import argparse, csv
from pathlib import Path
import cv2
import numpy as np
from scipy import ndimage as ndi
from skimage.feature import peak_local_max
from skimage.morphology import remove_small_objects
from skimage.segmentation import watershed, find_boundaries
from rbpms_counter_v2 import DEFAULTS, select_channel

FIELDS=['id','x_px','y_px','area_px2','mean_green','max_green','local_contrast',
        'circularity','solidity','vessel_overlap','class','review_note']


def read_png(path):
    raw=cv2.imdecode(np.fromfile(str(path),dtype=np.uint8),cv2.IMREAD_UNCHANGED)
    if raw is None: raise ValueError('Cannot open PNG: '+str(path))
    if raw.dtype!=np.uint8: raise ValueError('Expected 8-bit PNG')
    if raw.ndim==2: return np.stack([raw]*3,axis=-1)
    if raw.shape[2]<3: raise ValueError('Expected grayscale or RGB image')
    return cv2.cvtColor(raw[:,:,:3],cv2.COLOR_BGR2RGB)


def save_png(path,im):
    im=cv2.cvtColor(im,cv2.COLOR_RGB2BGR) if im.ndim==3 else im
    ok,encoded=cv2.imencode('.png',im)
    if not ok: raise OSError('Failed to encode '+str(path))
    encoded.tofile(str(path))


def segment(signal, floor=55, min_distance=8, min_area=40):
    smooth=cv2.GaussianBlur(signal,(0,0),1.1)
    background=cv2.GaussianBlur(smooth,(0,0),18)
    fg=(smooth>=floor)&((smooth.astype(float)-background)>=9)
    fg=remove_small_objects(fg,max_size=13)
    fg=ndi.binary_fill_holes(fg)
    fg=remove_small_objects(fg,max_size=13)
    distances=ndi.gaussian_filter(ndi.distance_transform_edt(fg),.8)
    peaks=peak_local_max(distances,min_distance=min_distance,threshold_abs=2.8,
                         labels=fg.astype(np.uint8),exclude_border=False)
    seed=np.zeros(fg.shape,dtype=np.uint8)
    if len(peaks): seed[peaks[:,0],peaks[:,1]]=1
    markers,_=ndi.label(seed)
    lbl=watershed(-distances,markers,mask=fg)
    areas=np.bincount(lbl.ravel())
    chosen=np.flatnonzero((areas>=min_area)&(areas<=1600)&(np.arange(len(areas))!=0))
    lut=np.zeros(len(areas),dtype=np.int32)
    lut[chosen]=np.arange(1,len(chosen)+1)
    return lut[lbl],background


def read_vessel_mask(mask_path,shape):
    if mask_path is None: return np.zeros(shape,dtype=bool)
    a=cv2.imdecode(np.fromfile(str(mask_path),dtype=np.uint8),cv2.IMREAD_GRAYSCALE)
    if a is None or a.shape!=shape:
        raise ValueError('Vessel mask must be a grayscale PNG with the SAME dimensions as image')
    return a>127


def classify_image(image_path,out,mask_path=None,show_ids=False,*,channel='green'):
    rgb=read_png(image_path)
    channel=select_channel(rgb,channel)
    signal=rgb[:,:,0 if channel=='red' else 1]
    labels,background=segment(signal,**DEFAULTS[channel])
    vessels=read_vessel_mask(mask_path,signal.shape)
    overlay=rgb.copy()
    class_mask=np.zeros(labels.shape,dtype=np.uint8)
    rows=[]
    for idx,sl in enumerate(ndi.find_objects(labels),1):
        if sl is None:continue
        binary=np.uint8(labels[sl]==idx)
        yy,xx=np.nonzero(binary)
        if len(xx)==0:continue
        x=float(xx.mean()+sl[1].start);y=float(yy.mean()+sl[0].start)
        n=int(len(xx))
        vals=signal[sl][binary>0]
        mean=float(vals.mean()); maximum=float(vals.max()); contrast=mean-float(background[sl][binary>0].mean())
        contour,_=cv2.findContours(binary,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        if not contour: continue
        biggest=max(contour,key=cv2.contourArea)
        perimeter=cv2.arcLength(biggest,True)
        circ=float(4*np.pi*n/(perimeter**2)) if perimeter>0 else 0.0
        hull=cv2.convexHull(np.column_stack([xx,yy]).astype(np.int32))
        solidity=float(n/max(cv2.contourArea(hull),1))
        overlap=float(vessels[sl][binary>0].mean())
        strong=(mean>=105 and circ>=.35 and solidity>=.76 and
                (overlap<.5 or mean>=150))
        weak=(mean>=65 and circ>=.26 and solidity>=.65 and n>=130 and contrast>=26 and overlap<.5)
        if strong:
            category='strong_candidate';code=1;note='review morphology especially on vessels'
        elif weak:
            category='weak_review';code=2;note='manual verification required; faint soma or artifact'
        else:
            category='rejected_review';code=3;note='possible artifact OR missed real cell; audit before exclusion'
        class_mask[sl][binary>0]=code
        rows.append(dict(id=idx,x_px=round(x,2),y_px=round(y,2),area_px2=n,
                         **{f'mean_{channel}':round(mean,2),f'max_{channel}':round(maximum,2)},
                         local_contrast=round(contrast,2),circularity=round(circ,3),
                         solidity=round(solidity,3),vessel_overlap=round(overlap,3),
                         **{'class':category},review_note=note))
    # Draw colored contours per class; yellow=weak, magenta=excluded, cyan=strong.
    for c,color in [(3,(255,0,255)),(2,(255,255,0)),(1,(0,255,255))]:
        boundary=find_boundaries(class_mask==c,mode='inner')
        overlay[boundary]=color
    for r in rows:
        x,y=int(round(r['x_px'])),int(round(r['y_px']))
        color={'strong_candidate':(0,255,255),'weak_review':(255,255,0),
               'rejected_review':(255,0,255)}[r['class']]
        cv2.circle(overlay,(x,y),2,color,-1)
        if show_ids:
            cv2.putText(overlay,str(r['id']),(x+3,y-3),cv2.FONT_HERSHEY_SIMPLEX,
                        .26,(255,255,255),1,cv2.LINE_AA)
    out.mkdir(parents=True,exist_ok=True)
    save_png(out/(image_path.stem+'_v3_overlay.png'),overlay)
    save_png(out/(image_path.stem+'_v3_class_mask.png'),class_mask)
    save_png(out/(image_path.stem+'_v3_labels16.png'),labels.astype(np.uint16))
    with (out/(image_path.stem+'_v3_cells.csv')).open('w',newline='',encoding='utf-8-sig') as f:
        fields=FIELDS if channel=='green' else [field.replace('_green','_red') for field in FIELDS]
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)
    counts={k:sum(r['class']==k for r in rows) for k in ['strong_candidate','weak_review','rejected_review']}
    return dict(image=image_path.name,channel=channel,**counts,total_candidates=len(rows),
                vessel_mask_used=bool(mask_path),status='REQUIRES MANUAL CELL-BY-CELL REVIEW')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',required=True,help='PNG image or root folder')
    p.add_argument('--output',required=True,help='Output folder, separate from original images')
    p.add_argument('--vessel-mask',default=None,help='Manual binary vessel mask PNG for SINGLE IMAGE only')
    p.add_argument('--suffix',default='_c1',help='Stem suffix used for recursive folder selection (default _c1)')
    p.add_argument('--show-ids',action='store_true')
    args=p.parse_args()
    src=Path(args.input);out=Path(args.output)
    if not src.exists():p.error('Input path not found')
    if src.is_dir() and args.vessel_mask:p.error('Do not reuse one vessel mask for an entire batch')
    if src.is_file():paths=[src]
    else: paths=sorted((f for f in src.rglob('*') if f.is_file() and f.suffix.lower()=='.png'
                        and f.stem.casefold().endswith(args.suffix.casefold())),key=lambda f:str(f).casefold())
    if not paths:p.error('No matching PNG files')
    report=[]
    for f in paths:
        rel=f.relative_to(src) if src.is_dir() else Path(f.name)
        try:
            result=classify_image(f,out/rel.parent,args.vessel_mask,args.show_ids)
            result['relative_path']=str(rel)
            print(str(rel),result['strong_candidate'],'strong;',result['weak_review'],'weak review;',
                  result['rejected_review'],'rejected review',flush=True)
        except Exception as e:
            result={k:'' for k in ['image','strong_candidate','weak_review','rejected_review',
                                   'total_candidates','vessel_mask_used']}
            result['image']=f.name;result['status']='ERROR: '+str(e)
            print('ERROR',rel,e,flush=True)
        result['relative_path']=str(rel)
        report.append(result)
    out.mkdir(parents=True,exist_ok=True)
    with (out/'summary_v3.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=['relative_path','image','strong_candidate','weak_review',
                              'rejected_review','total_candidates','vessel_mask_used','status'])
        writer.writeheader();writer.writerows(report)
    print('Processed',len(paths),'image(s). NOTE: These are candidate groups, not validated cell counts.')

if __name__=='__main__':main()

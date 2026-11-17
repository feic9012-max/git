r"""RBPMS V3 interactive review tool (Tkinter, Windows).

Open original PNG and the matching V3 *_v3_labels16.png + *_v3_cells.csv.
Only high-confidence candidates are counted initially; click to correct.
All edits are preserved in separate JSON/CSV/PNG review outputs.

Run from VS Code terminal:
    .\\.venv\\Scripts\\python.exe rbpms_review_gui.py
Optional:
    .\\.venv\\Scripts\\python.exe rbpms_review_gui.py --image "images\sample_c1.png" --results "results_v3_vessel"
"""
from __future__ import annotations
import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import numpy as np
import cv2
from PIL import Image, ImageTk
from scipy import ndimage as ndi

COLOR = {
    'strong_candidate': (0, 255, 255),  # cyan RGB
    'weak_review': (255, 235, 40),     # yellow RGB
    'rejected_review': (255, 70, 205),# magenta RGB
}

def read_png(path):
    arr = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if arr is None: raise ValueError(f'无法读取图片: {path}')
    if arr.ndim == 2:
        if arr.dtype != np.uint8:
            arr = cv2.convertScaleAbs(arr, alpha=255.0 / max(np.iinfo(arr.dtype).max, 1))
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2RGB)
    if arr.shape[2] == 4: arr = arr[:, :, :3]
    if arr.dtype != np.uint8: raise ValueError('原图需要 8-bit PNG；请使用 V3 分析时的原始 PNG')
    return cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)

def load_label(path):
    arr = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if arr is None or arr.ndim != 2: raise ValueError('无法读取单通道 V3 标签 PNG')
    return arr.astype(np.int32)

def save_png(path, rgb):
    arr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    ok, encoded = cv2.imencode('.png', arr)
    if not ok: raise OSError('无法编码 PNG')
    encoded.tofile(str(path))

def locate_outputs(image, result_folder):
    stem = image.stem
    labels = result_folder / (stem + '_v3_labels16.png')
    cells = result_folder / (stem + '_v3_cells.csv')
    if not labels.is_file() or not cells.is_file():
        raise FileNotFoundError('在指定结果文件夹中未找到同名的 _v3_labels16.png 和 _v3_cells.csv。\n请确认使用该原图生成的 V3 结果，并选择正确的 results_v3_vessel 文件夹。')
    return labels, cells

class ReviewModel:
    def __init__(self, image_path, results_dir):
        self.image_path = Path(image_path)
        self.result_dir = Path(results_dir)
        self.label_path, self.csv_path = locate_outputs(self.image_path, self.result_dir)
        self.rgb = read_png(self.image_path)
        self.labels = load_label(self.label_path)
        if self.rgb.shape[:2] != self.labels.shape: raise ValueError('原图与 V3 标签图尺寸不同，无法审核')
        self.h, self.w = self.labels.shape
        with self.csv_path.open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        self.candidates = {}
        for row in rows:
            idx = int(row['id'])
            if idx in self.candidates: raise ValueError('候选 ID 重复')
            self.candidates[idx] = row
        present = set(int(i) for i in np.unique(self.labels) if i != 0)
        if present != set(self.candidates):
            raise ValueError('细胞 CSV 和 label PNG 的候选 ID 不一致；请使用同一次 V3 运行结果')
        self.included = {idx for idx, r in self.candidates.items() if r['class'] == 'strong_candidate'}
        self.added = {}  # manual id (string) -> (x, y)
        self.next_id = 1
        self.undo = []
        self.redo = []
        self.events = []
        self.review_output = self.result_dir / 'manual_review'
        self.contours = {}
        for idx, sl in enumerate(ndi.find_objects(self.labels), start=1):
            if sl is None or idx not in self.candidates: continue
            binary = np.uint8(self.labels[sl] == idx)
            contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            polys=[]
            for poly in contours:
                shifted=poly.copy()
                shifted[:,:,0] += sl[1].start
                shifted[:,:,1] += sl[0].start
                polys.append(shifted)
            self.contours[idx]=polys
        self.display_overlay = True
        self.view = self.rgb.copy()
        self.rebuild_overlay()

    def snapshot(self):
        return (set(self.included), dict(self.added), self.next_id, len(self.events))

    def push(self):
        self.undo.append(self.snapshot())
        if len(self.undo) > 1000: self.undo.pop(0)
        self.redo.clear()

    def log(self, action, **data):
        self.events.append({'time': datetime.now().isoformat(timespec='seconds'), 'action': action, **data})

    def _restore(self, snap):
        included, added, next_id, n_events = snap
        self.included, self.added, self.next_id = set(included), dict(added), next_id
        self.events = self.events[:n_events]
        self.rebuild_overlay()

    def undo_one(self):
        if not self.undo: return False
        self.redo.append(self.snapshot())
        self._restore(self.undo.pop())
        return True

    def redo_one(self):
        if not self.redo: return False
        self.undo.append(self.snapshot())
        self._restore(self.redo.pop())
        return True

    def closest_manual(self, x, y, radius=13):
        matches = [( (x-mx)**2+(y-my)**2, mid) for mid, (mx,my) in self.added.items()]
        matches = [p for p in matches if p[0] <= radius*radius]
        return min(matches)[1] if matches else None

    def locate_candidate(self, x, y):
        idx = int(self.labels[y,x])
        if idx in self.candidates: return idx
        # Mouse may fall on a dark part of the soma / thin boundary. Check nearest label pixel.
        r = 7
        y0,y1=max(0,y-r),min(self.h,y+r+1)
        x0,x1=max(0,x-r),min(self.w,x+r+1)
        sub=self.labels[y0:y1,x0:x1]
        yy,xx=np.nonzero(sub)
        if yy.size == 0: return None
        dist=(yy+y0-y)**2+(xx+x0-x)**2
        best=int(np.argmin(dist))
        if int(dist[best]) > 7*7: return None
        nearest = int(sub[yy[best],xx[best]])
        return nearest if nearest in self.candidates else None

    def edit_at(self, x, y, mode):
        if not (0 <= x < self.w and 0 <= y < self.h): return '图像范围以外'
        mid = self.closest_manual(x,y)
        idx = self.locate_candidate(x,y)
        if mode == 'add':
            if mid is not None: return f'{mid} 已是人工补充细胞'
            if idx is not None:
                if idx in self.included: return f'#{idx} 已计入'
                self.push(); self.included.add(idx); self.log('include_candidate',id=idx)
                msg=f'#{idx} 已计入'
            else:
                self.push(); mid=f'M{self.next_id:04d}'; self.next_id+=1
                self.added[mid]=(x,y); self.log('add_manual',id=mid,x_px=x,y_px=y)
                msg=f'添加 {mid}'
        elif mode == 'remove':
            if mid is not None:
                self.push(); self.added.pop(mid); self.log('remove_manual',id=mid)
                msg=f'移除 {mid}'
            elif idx is not None:
                if idx not in self.included: return f'#{idx} 已排除'
                self.push(); self.included.remove(idx); self.log('exclude_candidate',id=idx)
                msg=f'#{idx} 已排除'
            else: return '附近没有可删除的候选或人工点'
        else: # toggle
            if mid is not None:
                self.push(); self.added.pop(mid); self.log('remove_manual',id=mid); msg=f'移除 {mid}'
            elif idx is not None:
                self.push()
                if idx in self.included:
                    self.included.remove(idx); self.log('exclude_candidate',id=idx); msg=f'#{idx} 已排除'
                else:
                    self.included.add(idx); self.log('include_candidate',id=idx); msg=f'#{idx} 已计入'
            else:
                self.push(); mid=f'M{self.next_id:04d}'; self.next_id+=1
                self.added[mid]=(x,y); self.log('add_manual',id=mid,x_px=x,y_px=y)
                msg=f'添加 {mid}'
        self.rebuild_overlay()
        return msg

    def counts(self):
        return len(self.included), len(self.added), len(self.included)+len(self.added)

    def rebuild_overlay(self):
        dst = self.rgb.copy()
        # Gray/cyan/yellow/magenta = original candidate class; included gets solid circle at centroid.
        for idx, row in self.candidates.items():
            cnt=self.contours.get(idx,[])
            col=COLOR.get(row['class'],(160,160,160))
            if idx not in self.included: col=tuple(int(v*.53) for v in col)
            cv2.drawContours(dst,cnt,-1,col,1)
            x,y=int(round(float(row['x_px']))),int(round(float(row['y_px'])))
            if idx in self.included:
                cv2.circle(dst,(x,y),3,(0,255,255),-1,cv2.LINE_AA)
            else:
                cv2.line(dst,(x-2,y-2),(x+2,y+2),(200,70,70),1,cv2.LINE_AA)
        for mid,(x,y) in self.added.items():
            cv2.circle(dst,(x,y),8,(255,255,255),1,cv2.LINE_AA)
            cv2.circle(dst,(x,y),3,(255,255,255),-1)
        self.view=dst

    def save(self):
        out=self.review_output
        out.mkdir(parents=True,exist_ok=True)
        stem=self.image_path.stem
        rows=[]
        for idx, row in sorted(self.candidates.items()):
            r=dict(row)
            r.update(source='v3_candidate',reviewed_include=int(idx in self.included),manual_id='')
            rows.append(r)
        for mid,(x,y) in self.added.items():
            rows.append(dict(id='',x_px=x,y_px=y,area_px2='',mean_green='',max_green='',
                local_contrast='',circularity='',solidity='',vessel_overlap='',
                **{'class':'manual_added'},review_note='manual click; no segmentation mask',
                source='manual_point',reviewed_include=1,manual_id=mid))
        headers=['id','manual_id','source','class','reviewed_include','x_px','y_px','area_px2',
                 'mean_green','max_green','local_contrast','circularity','solidity','vessel_overlap','review_note']
        with (out/(stem+'_reviewed_cells.csv')).open('w',newline='',encoding='utf-8-sig') as f:
            writer=csv.DictWriter(f,fieldnames=headers);writer.writeheader();writer.writerows(rows)
        incl,manual,total=self.counts()
        summary=out/(stem+'_review_summary.csv')
        with summary.open('w',newline='',encoding='utf-8-sig') as f:
            writer=csv.writer(f);writer.writerow(['image','original_strong','retained_candidates',
                'manual_added','final_count','reviewed_at'])
            writer.writerow([self.image_path.name,sum(r['class']=='strong_candidate' for r in self.candidates.values()),
                incl,manual,total,datetime.now().isoformat(timespec='seconds')])
        state={'format_version':1,'image':str(self.image_path),'v3_labels':str(self.label_path),
               'included_ids':sorted(self.included),'manual_added':self.added,
               'next_manual_id':self.next_id,'events':self.events}
        (out/(stem+'_review_session.json')).write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
        save_png(out/(stem+'_review_overlay.png'),self.view)
        return out, total

    def load_session(self, path):
        data=json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('format_version')!=1: raise ValueError('不支持的审核记录格式')
        if Path(data['image']).name != self.image_path.name: raise ValueError('审核记录的原图文件名不匹配')
        included=set(map(int,data['included_ids']))
        if not included.issubset(self.candidates): raise ValueError('审核记录中含有当前候选图不存在的 ID')
        self.included=included
        self.added={str(k):tuple(map(int,v)) for k,v in data['manual_added'].items()}
        self.next_id=int(data.get('next_manual_id',1))
        self.events=list(data.get('events',[]))
        self.undo.clear();self.redo.clear();self.rebuild_overlay()

class App:
    def __init__(self, root, image=None, results=None):
        self.root=root;root.title('RBPMS V3 细胞人工审核工具');root.geometry('1220x850')
        self.model=None
        self.mode=tk.StringVar(value='toggle')
        self.scale=1.; self.offset=(0.,0.)
        self.drag_start=None
        bar=ttk.Frame(root,padding=6);bar.pack(fill='x')
        ttk.Button(bar,text='打开原图及V3结果',command=self.open_dialog).pack(side='left',padx=3)
        ttk.Button(bar,text='加载上次审核',command=self.load).pack(side='left',padx=3)
        ttk.Button(bar,text='保存结果',command=self.save).pack(side='left',padx=3)
        ttk.Button(bar,text='撤销 Ctrl+Z',command=self.undo).pack(side='left',padx=3)
        ttk.Button(bar,text='重做 Ctrl+Y',command=self.redo).pack(side='left',padx=3)
        ttk.Button(bar,text='适合窗口',command=self.fit).pack(side='left',padx=3)
        modebar=ttk.Frame(root,padding=(8,2));modebar.pack(fill='x')
        ttk.Label(modebar,text='左键操作：').pack(side='left')
        for value,text in [('toggle','智能切换：候选取反/空白补点'),('add','添加：计入候选或空白补点'),('remove','删除：不计入候选/删除人工点')]:
            ttk.Radiobutton(modebar,text=text,variable=self.mode,value=value).pack(side='left',padx=8)
        self.count_var=tk.StringVar(value='尚未加载图像')
        ttk.Label(root,textvariable=self.count_var,font=('Segoe UI',12,'bold'),padding=5).pack(fill='x')
        self.canvas=tk.Canvas(root,bg='#161616',highlightthickness=0)
        self.canvas.pack(fill='both',expand=True)
        self.canvas.bind('<Button-1>',self.click)
        self.canvas.bind('<MouseWheel>',self.wheel)
        self.canvas.bind('<ButtonPress-3>',self.pan_start)
        self.canvas.bind('<B3-Motion>',self.pan_move)
        self.canvas.bind('<Configure>',lambda e:self.render() if self.model else None)
        root.bind('<Control-z>',lambda e:self.undo())
        root.bind('<Control-y>',lambda e:self.redo())
        root.bind('<Control-s>',lambda e:self.save())
        hint='青=强候选，黄=弱候选，紫=其他候选；亮点=已计入，暗叉=未计入，白圈=人工补点。滚轮缩放，按住鼠标右键拖动平移。'
        self.status=tk.StringVar(value=hint)
        ttk.Label(root,textvariable=self.status,padding=(7,4),wraplength=1100).pack(fill='x')
        if image and results:
            self.open_image(Path(image),Path(results))
        else:
            root.after(180,self.open_dialog)

    def open_dialog(self):
        image=filedialog.askopenfilename(title='选择未经标注的 RBPMS 原始 PNG',filetypes=[('PNG 图像','*.png')])
        if not image:return
        res=filedialog.askdirectory(title='选择该图的 V3 结果文件夹（包含 _v3_cells.csv 与 _v3_labels16.png）')
        if res:self.open_image(Path(image),Path(res))

    def open_image(self,image,result):
        try:
            self.model=ReviewModel(image,result)
        except Exception as ex:
            messagebox.showerror('读取失败',str(ex));return
        self.root.title('RBPMS V3 审核 | '+image.name)
        self.update_count();self.root.after(100,self.fit)
        self.status.set('已打开 '+image.name+'。默认仅强候选计入；黄色和紫色候选均可点击加入。请核查所有候选。')

    def update_count(self):
        if self.model:
            inc,manual,total=self.model.counts()
            self.count_var.set(f'已计入候选 {inc} + 人工补点 {manual} = 当前总数 {total}   |   V3 原始候选 {len(self.model.candidates)}')

    def fit(self):
        if not self.model:return
        self.root.update_idletasks()
        cw,ch=max(1,self.canvas.winfo_width()),max(1,self.canvas.winfo_height())
        self.scale=min(cw/self.model.w,ch/self.model.h,2.5)
        self.offset=((cw-self.model.w*self.scale)/2,(ch-self.model.h*self.scale)/2)
        self.render()

    def render(self):
        if not self.model:return
        cw,ch=max(1,self.canvas.winfo_width()),max(1,self.canvas.winfo_height())
        ox,oy=self.offset;s=self.scale
        iw=max(1,int(round(self.model.w*s)));ih=max(1,int(round(self.model.h*s)))
        # Resampling only for display; mouse clicks always map to original pixels.
        picture=Image.fromarray(self.model.view).resize((iw,ih),Image.Resampling.NEAREST if s>=1.5 else Image.Resampling.BILINEAR)
        self.photo=ImageTk.PhotoImage(picture)
        self.canvas.delete('all')
        self.canvas.create_image(ox,oy,anchor='nw',image=self.photo)

    def click(self,event):
        if not self.model:return
        x=int((event.x-self.offset[0])/self.scale);y=int((event.y-self.offset[1])/self.scale)
        if not (0<=x<self.model.w and 0<=y<self.model.h):return
        self.status.set(self.model.edit_at(x,y,self.mode.get()))
        self.update_count();self.render()

    def wheel(self,event):
        if not self.model:return
        factor=1.2 if event.delta>0 else 1/1.2
        new=max(.25,min(6.,self.scale*factor))
        ix=(event.x-self.offset[0])/self.scale
        iy=(event.y-self.offset[1])/self.scale
        self.offset=(event.x-ix*new,event.y-iy*new)
        self.scale=new;self.render()

    def pan_start(self,event):self.drag_start=(event.x,event.y,self.offset)
    def pan_move(self,event):
        if not self.drag_start:return
        x,y,(ox,oy)=self.drag_start
        self.offset=(ox+event.x-x,oy+event.y-y);self.render()
    def undo(self):
        if self.model and self.model.undo_one():self.update_count();self.render();self.status.set('已撤销')
    def redo(self):
        if self.model and self.model.redo_one():self.update_count();self.render();self.status.set('已重做')
    def save(self):
        if not self.model:return
        try:folder,total=self.model.save()
        except Exception as ex:messagebox.showerror('保存失败',str(ex));return
        self.status.set(f'已保存：{folder}  总数 {total}')
        messagebox.showinfo('已保存',f'人工修正后的数量：{total}\n结果保存在：\n{folder}\n\n请保留原始 V3 文件以便追溯。')
    def load(self):
        if not self.model:
            messagebox.showinfo('提示','请先打开原图和 V3 结果');return
        file=filedialog.askopenfilename(title='选择审核会话 JSON',filetypes=[('JSON 文件','*.json')])
        if not file:return
        try:self.model.load_session(file)
        except Exception as ex:messagebox.showerror('加载失败',str(ex));return
        self.update_count();self.render();self.status.set('审核记录已加载')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image',help='原始 PNG 路径')
    p.add_argument('--results',help='同名的 V3 输出文件夹')
    args=p.parse_args()
    if bool(args.image)!=bool(args.results):p.error('--image 和 --results 要一起提供，或都不提供')
    root=tk.Tk();App(root,args.image,args.results);root.mainloop()

if __name__=='__main__':main()

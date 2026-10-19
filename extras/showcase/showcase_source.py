"""Asset-only editor: actual timestamped screenshots + original music.

Final rendering requires a reviewed capture manifest with privacy_confirmed=true.
No UI or trading data are generated here. Inputs remain read-only.
"""
from __future__ import annotations
import argparse, hashlib, json, math, pathlib, shutil, statistics, subprocess, tempfile, time

OWNED_TEMP=pathlib.Path(tempfile.gettempdir())/'jev-showcase-assets'
REPO='https://github.com/mars-tw/jev-trading-command-center'
SR=48000

def run(args, cwd=None):
    if args[0]=='ffmpeg':
        process=subprocess.Popen(args,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8',errors='replace')
        print(json.dumps({'asset_process':'ffmpeg','pid':process.pid,'started':True}),flush=True)
        if cwd:(pathlib.Path(cwd)/'encode-process.json').write_text(json.dumps({'pid':process.pid,'hard_cutoff_seconds':600}),encoding='utf8')
        try:out,err=process.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            process.kill();process.communicate();raise
        if process.returncode:raise subprocess.CalledProcessError(process.returncode,args,out,err)
        return subprocess.CompletedProcess(args,process.returncode,out,err)
    return subprocess.run(args,cwd=cwd,capture_output=True,text=True,encoding='utf8',errors='replace',check=True,timeout=600)

def probe(path):
    return json.loads(run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)]).stdout)

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def within(child,parent):
    return child.resolve().is_relative_to(parent.resolve())

def load_segments(base,manifest):
    result=[]
    for spec in manifest['segments']:
        folder=(base/spec.get('folder','')).resolve()
        if not within(folder,base) or not folder.is_dir():raise ValueError('Frame folder outside provided root')
        frames=[]
        for image in folder.iterdir():
            if image.suffix.lower() not in ('.jpg','.jpeg','.png'):continue
            metadata=image.with_suffix('.json')
            data=json.loads(metadata.read_text(encoding='utf8'))
            timestamp=data.get('timestamp',data.get('epochseconds'))
            if not isinstance(timestamp,(int,float)) or not math.isfinite(timestamp):raise ValueError('Invalid timestamp')
            sequence=data.get('sequence')
            if not isinstance(sequence,int):raise ValueError('Invalid sequence')
            frames.append({'file':image,'timestamp':float(timestamp),'sequence':sequence})
        frames.sort(key=lambda f:f['sequence'])
        if len(frames)<2:raise ValueError('At least two real capture frames per segment are required')
        unique=[]
        for f in frames:
            if unique and f['timestamp']<unique[-1]['timestamp']:raise ValueError('Nonmonotonic capture timestamps')
            if unique and f['timestamp']==unique[-1]['timestamp']:unique[-1]=f
            else:unique.append(f)
        frames=unique
        original_first=frames[0]['timestamp'];original_count=len(frames)
        skip=spec.get('skip_frames',0);start_sequence=spec.get('start_sequence')
        if not isinstance(skip,int) or skip<0 or skip>len(frames)-2:raise ValueError('Invalid leading frame trim')
        if start_sequence is not None and (not isinstance(start_sequence,int) or start_sequence<0):raise ValueError('Invalid start sequence')
        frames=[f for i,f in enumerate(frames) if i>=skip and (start_sequence is None or f['sequence']>=start_sequence)]
        if len(frames)<2:raise ValueError('Trimming leaves fewer than two captured frames')
        gaps=[frames[i+1]['timestamp']-frames[i]['timestamp'] for i in range(len(frames)-1)]
        last_duration=statistics.median(gaps)
        measured=frames[-1]['timestamp']-frames[0]['timestamp']+last_duration
        requested=spec.get('duration_seconds')
        if requested is not None:
            requested=float(requested)-(frames[0]['timestamp']-float(spec.get('first_timestamp',original_first)))
            if not math.isfinite(requested) or abs(requested-measured)>.5:raise ValueError('Declared duration differs from capture by over 0.5 seconds')
            last_duration=requested-(frames[-1]['timestamp']-frames[0]['timestamp'])
            if last_duration < -0.000001:raise ValueError('Declared segment ends before its last frame')
            if abs(last_duration)<0.000001:last_duration=0.0
            measured=requested
        for i,f in enumerate(frames):f['duration']=gaps[i] if i<len(gaps) else last_duration
        result.append({'label':str(spec.get('label',spec.get('folder','Application')))[:90],'frames':frames,'duration':measured,'source_count':len(frames),'omitted_leading_frames':original_count-len(frames),'median_frame_interval':statistics.median(gaps)})
    if not result:raise ValueError('No segments')
    return result

def write_concat(path,segments,intro,outro):
    def entry(image,duration):
        # A high image demuxer timebase preserves the original sub-40ms VFR.
        name=image.resolve().as_posix().replace("'","'\\''")
        return ["file '"+name+"'",'option framerate 1000','duration '+format(duration,'.9f')]
    lines=['ffconcat version 1.0']
    if intro:lines+=entry(segments[0]['frames'][0]['file'],intro)
    for segment in segments:
        for f in segment['frames']:
            if f['duration']>0:lines+=entry(f['file'],f['duration'])
    if outro:lines+=entry(segments[-1]['frames'][-1]['file'],outro)
    lines+=entry(segments[-1]['frames'][-1]['file'],.001)
    path.write_bytes(('\n'.join(lines)+'\n').encode('utf8'))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--frames',type=pathlib.Path,required=True);parser.add_argument('--segments',type=pathlib.Path,required=True);parser.add_argument('--music',type=pathlib.Path,required=True);parser.add_argument('--output',type=pathlib.Path,required=True);parser.add_argument('--calibration',action='store_true');args=parser.parse_args()
    manifest=json.loads(args.segments.read_text(encoding='utf8'))
    if args.calibration:
        if manifest.get('calibration') is not True or not within(args.output,OWNED_TEMP):raise ValueError('Calibration output must remain in owned TEMP')
    elif manifest.get('privacy_confirmed') is not True:
        raise ValueError('Final export requires privacy_confirmed:true in the reviewed manifest')
    if args.output.exists():raise FileExistsError(args.output)
    segments=load_segments(args.frames,manifest);raw_duration=sum(s['duration'] for s in segments)
    music_meta=probe(args.music);music_duration=float(music_meta['format']['duration'])
    if args.calibration:target=raw_duration;intro=outro=0.0
    else:
        target=float(manifest.get('target_duration_seconds',music_duration));slack=target-raw_duration
        if slack<0 or slack>20:raise ValueError('Capture and target should leave 0..20 seconds for explicit title/outro holds')
        intro=min(2*4*60/116,slack*.45);outro=slack-intro
    OWNED_TEMP.mkdir(parents=True,exist_ok=True);work=OWNED_TEMP/('edit-'+str(time.time_ns()));work.mkdir()
    concat=work/'capture.ffconcat';write_concat(concat,segments,intro,outro)
    shutil.copy2(r'C:\Windows\Fonts\consola.ttf',work/'consola.ttf')
    (work/'title.txt').write_bytes(b'JEV Trading Command Center')
    (work/'subtitle.txt').write_bytes('Open-source application · DEMO'.encode('utf8'))
    (work/'repo.txt').write_bytes(str(manifest.get('repository_url',REPO)).encode('utf8'))
    (work/'demo.txt').write_bytes(b'DEMO / SYNTHETIC DATA' if manifest.get('data_mode')=='synthetic' else b'DEMO / SIMULATION')
    draw="drawtext=fontfile=consola.ttf:fontcolor=0xdbe4f5:fontsize=27:x=32:y=6:textfile=title.txt:enable='lt(t,"+str(intro)+")+gte(t,"+str(target-outro)+")',drawtext=fontfile=consola.ttf:fontcolor=0x65d6df:fontsize=17:x=32:y=35:textfile=subtitle.txt:enable='lt(t,"+str(intro)+")+gte(t,"+str(target-outro)+")',drawtext=fontfile=consola.ttf:fontcolor=0xa6bdd1:fontsize=16:x=32:y=h-25:textfile=repo.txt,drawtext=fontfile=consola.ttf:fontcolor=0x71e4d7:fontsize=16:x=w-tw-32:y=h-25:textfile=demo.txt"
    video='scale=1920:980:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:56:color=0x04060c,setsar=1,fps=30,format=yuv420p,'+draw
    audio='atrim=duration='+str(target)+',afade=t=out:st='+str(max(0,target-3.4))+':d='+str(min(target,3.4))+',alimiter=limit=0.84:level=false'
    filters='[0:v]'+video+'[v];[1:a]'+audio+'[a]'
    (work/'filter.txt').write_bytes(filters.encode('utf8'))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    encoded=work/'encoded-showcase.mp4'
    cmd=['ffmpeg','-hide_banner','-loglevel','error','-n','-safe','0','-f','concat','-i',str(concat),'-i',str(args.music.resolve()),'-filter_complex_script',str(work/'filter.txt'),'-map','[v]','-map','[a]','-t',str(target),'-c:v','libx264','-preset','medium','-crf','18','-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-ar',str(SR),'-movflags','+faststart','-map_metadata','-1',str(encoded)]
    completed=run(cmd,cwd=work)
    (work/'encode.stderr.log').write_text(completed.stderr,encoding='utf8')
    actual=probe(encoded);v=next(s for s in actual['streams'] if s['codec_type']=='video');a=next(s for s in actual['streams'] if s['codec_type']=='audio')
    report={'status':'PASS','calibration_only':args.calibration,'source':'actual screenshot frames with original timestamps; no generated UI','segments':[{'label':s['label'],'frames':s['source_count'],'omitted_leading_frames':s['omitted_leading_frames'],'duration_seconds':s['duration'],'median_frame_interval':s['median_frame_interval'],'first_frame_sha256':digest(s['frames'][0]['file']),'last_frame_sha256':digest(s['frames'][-1]['file'])} for s in segments],'raw_capture_duration_seconds':raw_duration,'intro_still_seconds':intro,'outro_still_seconds':outro,'target_duration_seconds':target,'actual_duration_seconds':float(actual['format']['duration']),'width':v['width'],'height':v['height'],'video_codec':v['codec_name'],'fps':v['avg_frame_rate'],'audio_codec':a['codec_name'],'music_sha256':digest(args.music),'sha256':digest(encoded),'metadata_stripped':True}
    if v['width']!=1920 or v['height']!=1080 or v['codec_name']!='h264' or a['codec_name']!='aac':raise ValueError('Unexpected output encoding')
    if not args.calibration:
        # Preview/thumbnail use only these confirmed real capture frames.
        start=min(intro+4,max(0,target-5))
        run(['ffmpeg','-hide_banner','-loglevel','error','-n','-ss',str(start),'-t','5','-i',str(encoded),'-filter_complex','fps=10,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a','-an',str(work/'preview.gif')])
        thumbnail_time=min(1,intro/2) if intro else start
        run(['ffmpeg','-hide_banner','-loglevel','error','-n','-ss',str(thumbnail_time),'-i',str(encoded),'-frames:v','1',str(work/'thumbnail.jpg')])
        for name in ['jev-preview.gif','jev-thumbnail.jpg']:
            if args.output.with_name(name).exists():raise FileExistsError(name)
        shutil.copy2(work/'preview.gif',args.output.with_name('jev-preview.gif'))
        shutil.copy2(work/'thumbnail.jpg',args.output.with_name('jev-thumbnail.jpg'))
    shutil.copy2(encoded,args.output)
    receipt=args.output.with_suffix('.qa.json');receipt.write_bytes((json.dumps(report,indent=2)+'\n').encode('utf8'))
    print(json.dumps(report))

if __name__=='__main__':main()

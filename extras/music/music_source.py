"""Original JEV 'Neon Circuit' cue. MIT licensed. No samples or external music.

Requires Python 3, numpy, FFmpeg/ffprobe on PATH. A deterministic seed, original
note sequences and simple oscillators generate the entire stereo recording.
Run: python music_source.py --output music.wav --temp-dir <owned-temp-folder>
"""
from __future__ import annotations
import argparse, hashlib, json, pathlib, re, subprocess, wave
import numpy as np

SR = 48000
BPM = 116
BEAT = 60 / BPM
BAR = 4 * BEAT
BARS = 34
DURATION = BARS * BAR
SEED = 20261019

def hz(midi):
    return 440.0 * 2.0 ** ((midi - 69) / 12.0)

def envelope(n, attack, release, decay=None):
    t = np.arange(n, dtype=np.float64) / SR
    out = np.ones(n)
    a, r = min(n, int(attack * SR)), min(n, int(release * SR))
    if a: out[:a] *= np.sin(np.linspace(0, np.pi / 2, a)) ** 2
    if r: out[-r:] *= np.sin(np.linspace(np.pi / 2, 0, r)) ** 2
    if decay: out *= np.exp(-t / decay)
    return out

def synthesize():
    rng = np.random.default_rng(SEED)
    n = round(DURATION * SR)
    mix = np.zeros((n, 2), dtype=np.float64)
    shimmer = np.zeros_like(mix)
    def add(sound, start, gain=1.0, pan=0.0, bus=mix):
        a = max(0, round(start * SR)); b = min(n, a + len(sound))
        if b <= a: return
        sound = sound[:b-a] * gain
        bus[a:b, 0] += sound * np.sqrt((1-pan)/2)
        bus[a:b, 1] += sound * np.sqrt((1+pan)/2)
    def tone(note, duration, flavor='pluck'):
        t = np.arange(max(1, round(duration * SR))) / SR
        f = hz(note)
        if flavor == 'pad':
            y = (.64*np.sin(2*np.pi*f*.9984*t) + .64*np.sin(2*np.pi*f*1.0016*t+.24)
                 + .12*np.sin(2*np.pi*f*2*t+.6) + .045*np.sin(2*np.pi*f*3*t))
            return y * envelope(len(t), .58, .80)
        if flavor == 'bass':
            y = np.sin(2*np.pi*f*t) + .22*np.sin(2*np.pi*f*2*t) + .07*np.sin(2*np.pi*f*3*t)
            return y * envelope(len(t), .008, .045, .32)
        y = np.sin(2*np.pi*f*t) + .22*np.sin(2*np.pi*f*2*t+.12) + .075*np.sin(2*np.pi*f*3*t)
        return y * envelope(len(t), .006, .10, .21 if flavor == 'arp' else .42)
    def kick():
        t=np.arange(round(.36*SR))/SR
        phase=2*np.pi*(48*t+67*.018*(1-np.exp(-t/.018)))
        return np.sin(phase)*np.exp(-t/0.095)*envelope(len(t),.0015,.045) + .025*rng.normal(size=len(t))*np.exp(-t/.004)
    def hat(opened=False):
        duration=.22 if opened else .085; t=np.arange(round(duration*SR))/SR
        noise=rng.normal(size=len(t)); high=noise-np.convolve(noise,np.ones(7)/7,'same')
        metallic=.08*np.sin(2*np.pi*7100*t)*np.sin(2*np.pi*9800*t)
        return (high*.4+metallic)*np.exp(-t/(.043 if opened else .018))*envelope(len(t),.001,.015)
    def snare():
        t=np.arange(round(.20*SR))/SR; noise=rng.normal(size=len(t))
        band=noise-np.convolve(noise,np.ones(19)/19,'same')
        clap=band*.25*(np.exp(-t/.045)+.6*np.exp(-((t-.012)/.007)**2))
        body=.15*np.sin(2*np.pi*185*t)*np.exp(-t/.038)
        return (clap+body)*envelope(len(t),.001,.025)
    # Two-bar harmonic cells: Dm9 / Bbmaj9 / Fmaj9 / Cadd9.
    chords=[([50,53,57,60,64],38),([46,50,53,57,60],34),([53,57,60,64,67],41),([48,52,55,62,64],36)]
    motif=[[(0,74,.72),(1.5,69,.46),(2.5,72,.43),(3.25,76,.48)],
           [(0,74,.68),(1.5,72,.45),(2.5,69,.44),(3.25,65,.52)],
           [(0,69,.72),(1.5,72,.46),(2.5,76,.43),(3.25,79,.46)],
           [(0,76,.70),(1.5,74,.44),(2.5,72,.43),(3.25,67,.52)]]
    arp_pattern=[0,2,4,1,3,2,4,1]
    for bar in range(BARS):
        start=bar*BAR; chord,root=chords[(bar//2)%4]
        intro=bar<4; outro=bar>=28
        energy=.60 if intro else .76 if outro else 1.0
        # Long notes overlap gently; independent stereo voicings avoid mono haze.
        if bar % 2 == 0:
            for j,note in enumerate(chord): add(tone(note,2*BAR+.65,'pad'),start,.022*energy,(j-2)*.29)
        if bar>=2 and bar<32:
            for k in range(8):
                note=chord[arp_pattern[k]]+12
                if bar%8>=6 and k in (5,7): note-=12
                add(tone(note,.43,'arp'),start+k*.5*BEAT,.038*energy,(-.55 if k%2 else .55),shimmer)
        if 4<=bar<30:
            for beat,note,duration in motif[(bar//2)%4]:
                if bar%2==0 or beat in (0,2.5): add(tone(note,duration,'lead'),start+beat*BEAT,.066*energy,-.12 if bar%2 else .12,shimmer)
        if 2<=bar<32:
            for k,offset in enumerate((0,.75,1.5,2,2.75,3.5)):
                note=root+(12 if k==5 and bar%2 else 0)
                add(tone(note,.25 if k%2 else .33,'bass'),start+offset*BEAT,.145*energy)
        if 2<=bar<32:
            beats=(0,2) if intro or bar>=30 else (0,1,2,3)
            for beat in beats: add(kick(),start+beat*BEAT,.30*energy)
            if bar>=4:
                for beat in (1,3): add(snare(),start+beat*BEAT,.17*energy,.05)
                for k in range(8): add(hat(k in (3,7) and bar%2==1),start+(k*.5)*BEAT,.045 if k%2 else .030,(-.37 if k%2 else .37))
        # Small upper-register answer every eight bars, composed from the chord.
        if bar in (11,19,27):
            for k,note in enumerate((chord[4]+12,chord[3]+12,chord[2]+12,chord[0]+12)):
                add(tone(note,.36,'lead'),start+(2+k*.5)*BEAT,.036,(-.4+k*.27),shimmer)
    # Dotted-eighth ping-pong echoes are generated from this original material.
    mix+=shimmer
    for delay,gain in ((.75*BEAT,.25),(1.5*BEAT,.11),(2.25*BEAT,.045)):
        shift=round(delay*SR)
        mix[shift:]+=shimmer[:-shift,::-1]*gain
    # Short stereo room taps for pads/synths; all computed, no impulse samples.
    dry=mix.copy()
    for delay,gain in ((.047,.07),(.081,.05),(.127,.035)):
        shift=round(delay*SR);mix[shift:]+=dry[:-shift,::-1]*gain
    mix=np.tanh(mix*1.18)/1.18
    fade=np.ones(n);a=round(1.7*SR);b=round(3.8*SR)
    fade[:a]=np.sin(np.linspace(0,np.pi/2,a))**2;fade[-b:]=np.sin(np.linspace(np.pi/2,0,b))**2
    mix*=fade[:,None]
    mix-=mix.mean(axis=0)
    mix*=.88/max(np.max(np.abs(mix)),.0001)
    return mix.astype(np.float32)

def run(args):
    return subprocess.run(args,capture_output=True,text=True,encoding='utf8',errors='replace',check=True)

def loudness(path):
    p=run(['ffmpeg','-hide_banner','-nostats','-i',str(path),'-af','loudnorm=I=-18:TP=-1.5:LRA=8:print_format=json','-f','null','-'])
    blocks=re.findall(r'\{\s*"input_i".*?\}',p.stderr,re.S)
    if not blocks: raise RuntimeError('No loudness report')
    return json.loads(blocks[-1])

def main():
    a=argparse.ArgumentParser();a.add_argument('--output',type=pathlib.Path,required=True);a.add_argument('--temp-dir',type=pathlib.Path,required=True);args=a.parse_args()
    args.temp_dir.mkdir(parents=True,exist_ok=True);args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.output.exists(): raise FileExistsError(args.output)
    audio=synthesize();raw=args.temp_dir/'neon-circuit-original-raw.wav'
    if raw.exists(): raise FileExistsError(raw)
    with wave.open(str(raw),'wb') as f:
        f.setnchannels(2);f.setsampwidth(2);f.setframerate(SR);f.writeframes((np.clip(audio,-1,1)*32767).astype('<i2').tobytes())
    first=loudness(raw)
    filt=('highpass=f=28,lowpass=f=16000,'
          'loudnorm=I=-18:TP=-1.5:LRA=8:measured_I='+first['input_i']+':measured_TP='+first['input_tp']+
          ':measured_LRA='+first['input_lra']+':measured_thresh='+first['input_thresh']+':offset='+first['target_offset']+':linear=true')
    run(['ffmpeg','-hide_banner','-loglevel','error','-i',str(raw),'-af',filt,'-ar',str(SR),'-ac','2','-c:a','pcm_s24le',str(args.output)])
    final=loudness(args.output)
    probe=json.loads(run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_name,sample_rate,channels,bits_per_sample','-of','json',str(args.output)]).stdout)
    passed=abs(float(final['input_i'])+18)<=.7 and float(final['input_tp']) < -1.0
    report={'title':'Neon Circuit','original_procedural_composition':True,'seed':SEED,'bpm':BPM,'bars':BARS,'duration_seconds':float(probe['format']['duration']),'sample_rate':SR,'channels':2,'loudness_lufs':float(final['input_i']),'true_peak_dbtp':float(final['input_tp']),'loudness_range_lu':float(final['input_lra']),'sha256':hashlib.sha256(args.output.read_bytes()).hexdigest(),'loudness_target_lufs':-18,'qa':'PASS' if passed else 'REVIEW','no_external_samples':True}
    (args.output.parent/'music-qa.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print(json.dumps(report))
    if not passed: raise RuntimeError('Loudness QA target not met')

if __name__=='__main__': main()

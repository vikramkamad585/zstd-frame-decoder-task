import random, subprocess, struct, sys
sys.argv=['x','/tmp/o','1','1','hidden']
src=open('gen.py').read().split('cat_levels()\ncat_fast()')[0]
exec(src)
import os
for wl,m,size,bmax in [(27,1,3000,None),(27,7,3000,None),(20,0,140000,140000),(20,0,200000,200000),(10,0,4000,4000)]:
  for cs in (False,True):
    r=random.Random(5); d=r.randbytes(size)
    f=raw_frame(r,d,window_log=wl,mantissa=m,fcs_size=4,checksum=cs,max_block=bmax)
    open('/tmp/f.zst','wb').write(f)
    p=subprocess.run([ZSTD,'-d','-c','/tmp/f.zst'],capture_output=True)
    print(wl,m,size,cs,p.returncode,p.stderr.decode().strip()[-90:])

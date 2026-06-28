#!/usr/bin/env python3
"""Daily E1 Pro alert summary. Counts today's camera emails, grabs first snapshot.
Run by daily cron at 23:00.
"""
import subprocess, os, json, sys, re
from datetime import datetime, timezone, timedelta

HIMALAYA = 'himalaya'
OUTPUT_DIR = '/home/xing/ftp_uploads'
os.makedirs(OUTPUT_DIR, exist_ok=True)

def himalaya_json(args):
    result = subprocess.run(
        [HIMALAYA] + args + ['--output', 'json'],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return None
    out = result.stdout
    idx = min(out.find('['), out.find('{'))
    if idx == -1:
        return None
    idx2 = out.rfind(']') if out[idx] == '[' else out.rfind('}')
    if idx2 == -1:
        return None
    try:
        return json.loads(out[idx:idx2+1])
    except:
        return None

def main():
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    # Get recent emails (last 50 should cover a day)
    msgs = himalaya_json(['envelope', 'list', '--page-size', '50'])
    if not msgs:
        print("No emails found")
        return
    
    camera_msgs = []
    for m in msgs:
        subj = m.get('subject', '').lower()
        date = m.get('date', '')
        if any(kw in subj for kw in ['motion', 'detected', 'alarm', 'alert', 'person', 'vehicle', 'pet', 'human']):
            if today in date:
                camera_msgs.append(m)
    
    if not camera_msgs:
        print(f"{today}: No E1 Pro alerts today")
        return
    
    print(f"📷 E1 Pro Daily Report — {today}")
    print(f"Total alerts: {len(camera_msgs)}")
    
    # Download attachment from first alert only
    first = camera_msgs[0]
    mid = first['id']
    subj = first.get('subject', '?')
    t = first.get('date', '?')
    
    print(f"\nSample: [{t}] {subj}")
    
    r = subprocess.run(
        [HIMALAYA, 'attachment', 'download', str(mid), '--dir', OUTPUT_DIR],
        capture_output=True, text=True, timeout=30
    )
    for line in (r.stdout + r.stderr).split('\n'):
        match = re.search(r'Downloaded.*?:\s*(/\S+)', line)
        if match:
            path = match.group(1)
            print(f"MEDIA:{path}")
    
    # List all alert times
    print(f"\nAll alerts today:")
    for m in camera_msgs:
        print(f"  {m.get('date', '?')[:19]} — {m.get('subject', '?')[:80]}")

if __name__ == '__main__':
    main()

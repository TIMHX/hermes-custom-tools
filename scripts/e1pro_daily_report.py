#!/usr/bin/env python3
"""Daily E1 Pro alert summary. Counts today's camera emails, grabs snapshots.
Run by daily cron at 23:00 EDT.
"""
import subprocess, os, json, sys, re, tempfile, shutil
from datetime import datetime
from zoneinfo import ZoneInfo

HIMALAYA = 'himalaya'
OUTPUT_DIR = '/home/xing/ftp_uploads'
IMAGE_EXTS = ('.jpg', '.jpeg', '.jfif', '.png', '.gif', '.bmp', '.webp')

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

def export_images(msg_id):
    """Export message via himalaya and extract inline images + attachments.
    Returns list of absolute file paths saved to OUTPUT_DIR."""
    try:
        tmpdir = tempfile.mkdtemp(prefix='e1pro_')
        r = subprocess.run(
            [HIMALAYA, 'message', 'export', str(msg_id), '-d', tmpdir],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode != 0:
            return []

        images = []
        for f in os.listdir(tmpdir):
            if f.lower().endswith(IMAGE_EXTS):
                src = os.path.join(tmpdir, f)
                dst_name = f"e1pro_{msg_id}_{f}"
                dst = os.path.join(OUTPUT_DIR, dst_name)
                shutil.copy2(src, dst)
                images.append(dst)
        return images
    finally:
        if 'tmpdir' in dir():
            shutil.rmtree(tmpdir, ignore_errors=True)

def main():
    today = datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')

    # Get recent emails (last 50 should cover a day)
    msgs = himalaya_json(['envelope', 'list', '--page-size', '50'])
    if not msgs:
        print("No emails found")
        return

    camera_msgs = []
    for m in msgs:
        subj = m.get('subject', '').lower()
        date = m.get('date', '')
        if any(kw in subj for kw in ['motion', 'detected', 'alarm', 'alert',
                                       'person', 'vehicle', 'pet', 'human']):
            if today in date:
                camera_msgs.append(m)

    if not camera_msgs:
        print(f"{today}: No E1 Pro alerts today")
        return

    print(f"📷 E1 Pro Daily Report — {today}")
    print(f"Total alerts: {len(camera_msgs)}")

    # Grab snapshot from the most recent alert
    first = camera_msgs[0]
    mid = first['id']
    subj = first.get('subject', '?')
    t = first.get('date', '?')

    print(f"\nLatest: [{t}] {subj}")

    images = export_images(mid)
    if images:
        print(f"📸 {len(images)} snapshot(s):")
        for img in images:
            print(f"MEDIA:{img}")
    else:
        print("(no snapshot in this email)")

    # List all alert times
    print(f"\nAll alerts today:")
    for m in camera_msgs:
        print(f"  {m.get('date', '?')[:19]} — {m.get('subject', '?')[:80]}")

if __name__ == '__main__':
    main()

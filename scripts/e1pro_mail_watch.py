#!/usr/bin/env python3
"""Monitor Gmail for Reolink camera alert emails.
Checks unseen emails, downloads snapshot attachments, reports paths.
"""
import subprocess, os, json, sys, time, re

HIMALAYA = 'himalaya'
OUTPUT_DIR = '/home/xing/ftp_uploads'
os.makedirs(OUTPUT_DIR, exist_ok=True)

def himalaya_json(args):
    """Run himalaya and parse JSON, stripping WARN lines."""
    result = subprocess.run(
        [HIMALAYA] + args + ['--output', 'json'],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return None
    # Strip stderr WARN lines that may be mixed into stdout
    out = result.stdout
    # Find first '[' or '{'
    idx = min(out.find('['), out.find('{'))
    if idx == -1:
        return None
    if out[idx] == '[':
        idx2 = out.rfind(']')
        json_str = out[idx:idx2+1]
    else:
        idx2 = out.rfind('}')
        json_str = out[idx:idx2+1]
    if idx2 == -1:
        return None
    try:
        return json.loads(json_str)
    except:
        return None

def main():
    # Get unseen emails
    msgs = himalaya_json(['envelope', 'list', '--page-size', '5'])
    if not msgs:
        return
    
    camera_msgs = []
    for m in msgs:
        subj = m.get('subject', '').lower()
        if any(kw in subj for kw in ['motion', 'detected', 'alarm', 'alert', 'person', 'vehicle', 'pet', 'human']):
            camera_msgs.append(m)
    
    if not camera_msgs:
        return
    
    all_files = []
    for m in camera_msgs:
        mid = m['id']
        subj = m.get('subject', '?')
        date = m.get('date', '?')
        print(f"[{date}] 📧 {subj}")
        
        # Download attachments
        r = subprocess.run(
            [HIMALAYA, 'attachment', 'download', str(mid), '--dir', OUTPUT_DIR],
            capture_output=True, text=True, timeout=30
        )
        for line in (r.stdout + r.stderr).split('\n'):
            match = re.search(r'Downloaded.*?:\s*(/\S+)', line)
            if match:
                path = match.group(1)
                all_files.append(path)
                print(f"  📎 {path}")
        
        # Mark as read
        subprocess.run(
            [HIMALAYA, 'flag', 'add', str(mid), '--flag', 'seen'],
            capture_output=True, timeout=10
        )
    
    if all_files:
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        print(f"\n[{ts}] 🔔 E1 Pro ALERT — {len(all_files)} snapshot(s)")
        for f in all_files:
            print(f"MEDIA:{f}")

if __name__ == '__main__':
    main()

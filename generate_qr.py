"""
generate_qr.py — Magic Auto Center QR sticker generator
QR codes open directly in Telegram — no website needed.

Usage:
    python generate_qr.py          # all active jobs
    python generate_qr.py RO-1043  # one job
"""
import os, sys, webbrowser, urllib.parse
import database as db

BOT_USERNAME = os.environ.get("BOT_USERNAME", "YourBotUsername")
OUTPUT = "qr_stickers.html"


def qr_url(data, size=200):
    enc = urllib.parse.quote(data, safe="")
    return f"https://api.qrserver.com/v1/create-qr-code/?size={size}x{size}&data={enc}&color=0f172a&bgcolor=ffffff&margin=10"


def generate(jobs):
    cards = ""
    for j in jobs:
        link  = f"https://t.me/{BOT_USERNAME}?start={j['id']}"
        cards += f"""
        <div class="card">
          <div class="top-bar">
            <span class="brand">🔧 MAGIC AUTO CENTER</span>
            <span class="ro">{j['id']}</span>
          </div>
          <div class="car">{j['car']}</div>
          <div class="plate">{j['plate'] or '&nbsp;'}</div>
          <div class="client">{j['client'] or '&nbsp;'}</div>
          <img class="qr" src="{qr_url(link)}" alt="QR">
          <div class="instruction">Scan to clock in &amp; out</div>
          <div class="works">{j['works'] or '&nbsp;'}</div>
        </div>"""

    html = f"""<!DOCTYPE html><html lang="en"><head>
  <meta charset="utf-8"><title>Magic Auto Center — QR Stickers</title>
  <style>
    @page {{ size: Letter; margin: 12mm; }}
    * {{ box-sizing:border-box; margin:0; padding:0; }}
    body {{ font-family: Arial, sans-serif; background:#f1f5f9; padding:16px; }}
    h1 {{ font-size:12px; color:#64748b; text-align:center; margin-bottom:14px; font-weight:400; }}
    .grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
    .card {{ background:#fff; border:2px solid #0f172a; border-radius:12px; overflow:hidden;
             text-align:center; break-inside:avoid; page-break-inside:avoid; }}
    .top-bar {{ background:#0f172a; color:#fff; padding:10px 12px;
                display:flex; justify-content:space-between; align-items:center; }}
    .brand {{ font-size:7px; letter-spacing:1.5px; opacity:.7; }}
    .ro {{ font-size:16px; font-weight:900; letter-spacing:1px; }}
    .car {{ font-size:14px; font-weight:800; color:#0f172a; padding:10px 12px 2px; }}
    .plate {{ font-size:12px; color:#475569; letter-spacing:3px; font-weight:700; padding:0 12px 4px; }}
    .client {{ font-size:11px; color:#94a3b8; padding:0 12px 6px; }}
    .qr {{ width:150px; height:150px; display:block; margin:0 auto 8px; }}
    .instruction {{ font-size:11px; font-weight:700; color:#0f172a; margin-bottom:5px; }}
    .works {{ font-size:9px; color:#94a3b8; padding:0 10px 10px; font-style:italic; line-height:1.4; }}
    @media print {{ body {{ background:white; padding:0; }} }}
  </style></head><body>
  <h1>Magic Auto Center — Print &amp; attach to windshield · {len(jobs)} job(s)</h1>
  <div class="grid">{cards}</div>
</body></html>"""

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ Saved: {OUTPUT}  →  Ctrl+P to print")
    return OUTPUT


def main():
    db.init_db()
    if len(sys.argv) > 1:
        job = db.get_job(sys.argv[1].upper())
        jobs = [job] if job else []
        if not jobs:
            print(f"❌ Job {sys.argv[1]} not found.")
            sys.exit(1)
    else:
        jobs = db.get_all_jobs("active")
        if not jobs:
            print("❌ No active jobs.")
            sys.exit(1)

    print(f"🖨  Generating {len(jobs)} sticker(s)...")
    path = generate(jobs)
    try:
        webbrowser.open(f"file://{os.path.abspath(path)}")
    except Exception:
        pass

if __name__ == "__main__":
    main()

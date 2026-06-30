import urllib.request
import os

def download_file(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f"Downloading {url} to {dest}...")
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req) as response, open(dest, 'wb') as out_file:
            data = response.read()
            out_file.write(data)
        print(f"[OK] Successfully downloaded {dest} ({len(data)} bytes)")
    except Exception as e:
        print(f"[FAIL] Failed to download {url}: {e}")

if __name__ == "__main__":
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    vendor_dir = os.path.join(base_dir, "web", "static", "vendor")
    
    # 1. 下载 ECharts
    download_file(
        "https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js",
        os.path.join(vendor_dir, "echarts.min.js")
    )
    
    # 2. 下载 Lucide Icons
    download_file(
        "https://unpkg.com/lucide@0.294.0/dist/umd/lucide.min.js",
        os.path.join(vendor_dir, "lucide.min.js")
    )

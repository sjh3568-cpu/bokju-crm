#!/usr/bin/env python
"""HTML 파일을 고해상도 PNG로 출력한다 (포스터·배너·표지·카드용).

    python tools/render_png.py 입력.html 출력.png [--width 1200] [--scale 2]

개발 PC에서만 쓴다 (NAS 컨테이너에는 브라우저가 없다).

한글이 깨지지 않게 하려면 HTML 안에서 폰트를 직접 지정해야 한다.
이 저장소는 Pretendard 파일을 싣고 있지 않으므로, PC에 Pretendard가 없으면
'맑은 고딕'으로 떨어진다. PC마다 글자가 달라지는 것을 막으려면
HTML 안에 @font-face 로 웹폰트를 넣거나 'Malgun Gothic' 을 명시한다.
"""
import argparse
import pathlib
import sys

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright 가 없습니다:  pip install playwright  &&  playwright install chromium")


def render(html_path, out_path, width, scale):
    url = pathlib.Path(html_path).resolve().as_uri()
    with sync_playwright() as p:
        # 번들 크로미움이 없으면 PC에 깔린 크롬/엣지를 빌려 쓴다.
        for kwargs in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
            try:
                browser = p.chromium.launch(**kwargs)
                break
            except Exception:
                continue
        else:
            sys.exit("크로미움·크롬·엣지 중 아무것도 못 띄웠습니다.  playwright install chromium")

        page = browser.new_context(
            viewport={"width": width, "height": 800},
            device_scale_factor=scale,
        ).new_page()
        page.goto(url)
        page.wait_for_load_state("networkidle")   # 웹폰트·이미지가 다 뜬 뒤에 찍는다
        page.screenshot(path=out_path, full_page=True)
        browser.close()
    print(f"저장: {out_path}  ({width}px × {scale}배)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("html")
    ap.add_argument("png")
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--scale", type=int, default=2)
    a = ap.parse_args()
    render(a.html, a.png, a.width, a.scale)

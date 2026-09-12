#!/usr/bin/env python3
"""Build the imagery-detail comparison page from the sample plates.

The samples are embedded as data URIs: an artifact may not load an image from
anywhere, so the pictures have to travel with the page.
"""

import base64
import os

SAMPLES = "satellite/samples"
OUT = os.environ.get("DETAIL_PAGE_OUT", "satellite/samples/detail.html")


def uri(name):
    with open(os.path.join(SAMPLES, f"{name}.jpg"), "rb") as f:
        return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()


PAGE = """<title>Does z14 Buy Anything?</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
  :root {{
    --ground:   #f2f4f0;
    --panel:    #ffffff;
    --ink:      #151b17;
    --muted:    #5f6b62;
    --rule:     #d3dbd3;
    --field:    #3f6b3a;
    --earth:    #8a5330;
    --plate-bg: #e7ebe5;
    --shadow:   0 1px 2px rgba(21,27,23,.08), 0 8px 24px rgba(21,27,23,.06);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --ground:   #11150f;
      --panel:    #181d16;
      --ink:      #e8ede6;
      --muted:    #9aa798;
      --rule:     #2c3429;
      --field:    #8fc184;
      --earth:    #d09765;
      --plate-bg: #0c0f0a;
      --shadow:   0 1px 2px rgba(0,0,0,.5), 0 10px 30px rgba(0,0,0,.35);
    }}
  }}
  :root[data-theme="dark"] {{
    --ground:   #11150f;
    --panel:    #181d16;
    --ink:      #e8ede6;
    --muted:    #9aa798;
    --rule:     #2c3429;
    --field:    #8fc184;
    --earth:    #d09765;
    --plate-bg: #0c0f0a;
    --shadow:   0 1px 2px rgba(0,0,0,.5), 0 10px 30px rgba(0,0,0,.35);
  }}

  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--ground);
    color: var(--ink);
    font-family: "Source Serif 4", Georgia, "Times New Roman", serif;
    font-size: 17px;
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{
    max-width: 1080px;
    margin: 0 auto;
    padding: 56px 24px 96px;
    display: flex;
    flex-direction: column;
    gap: 56px;
  }}
  h1, h2, .eyebrow, .plate-label, .stat-n, .stat-k, th {{
    font-family: Archivo, "Helvetica Neue", Arial, sans-serif;
  }}
  .eyebrow {{
    font-size: 12px;
    font-weight: 600;
    letter-spacing: .14em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0 0 10px;
  }}
  h1 {{
    font-size: clamp(32px, 5.2vw, 50px);
    font-weight: 700;
    line-height: 1.06;
    letter-spacing: -.02em;
    margin: 0 0 18px;
    text-wrap: balance;
  }}
  h2 {{
    font-size: clamp(21px, 2.6vw, 26px);
    font-weight: 600;
    letter-spacing: -.01em;
    margin: 0 0 6px;
    text-wrap: balance;
  }}
  p {{ margin: 0 0 14px; max-width: 66ch; }}
  p:last-child {{ margin-bottom: 0; }}
  .lede {{ font-size: 19px; color: var(--muted); max-width: 62ch; }}
  strong {{ font-weight: 600; }}

  section {{ display: flex; flex-direction: column; gap: 20px; }}
  .sub {{ color: var(--muted); margin: 0; max-width: 66ch; }}

  /* Survey plates: the pictures are the brightest thing on the page. */
  .plates {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 18px;
  }}
  figure {{ margin: 0; display: flex; flex-direction: column; gap: 8px; }}
  .plate-label {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: .1em;
    text-transform: uppercase;
  }}
  .plate-label .mpp {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-weight: 400;
    letter-spacing: 0;
    text-transform: none;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }}
  .plate {{
    background: var(--plate-bg);
    border: 1px solid var(--rule);
    border-radius: 3px;
    overflow: hidden;
    line-height: 0;
    box-shadow: var(--shadow);
  }}
  .plate img {{ width: 100%; height: auto; display: block; }}
  figcaption {{
    font-size: 14.5px;
    color: var(--muted);
    line-height: 1.45;
  }}
  figure.pick .plate {{ border-color: var(--field); border-width: 2px; }}
  figure.pick .plate-label {{ color: var(--field); }}

  .verdict {{
    background: var(--panel);
    border: 1px solid var(--rule);
    border-left: 3px solid var(--field);
    border-radius: 4px;
    padding: 24px 26px;
    box-shadow: var(--shadow);
  }}
  .verdict h2 {{ margin-bottom: 10px; }}

  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 15.5px;
  }}
  .scroll {{ overflow-x: auto; }}
  th, td {{
    text-align: left;
    padding: 11px 14px 11px 0;
    border-bottom: 1px solid var(--rule);
    vertical-align: top;
  }}
  th {{
    font-size: 12px;
    font-weight: 600;
    letter-spacing: .1em;
    text-transform: uppercase;
    color: var(--muted);
  }}
  td.num {{
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }}
  .no {{ color: var(--earth); }}
  .yes {{ color: var(--field); }}

  footer {{
    border-top: 1px solid var(--rule);
    padding-top: 20px;
    font-size: 14px;
    color: var(--muted);
  }}
  footer p {{ max-width: 76ch; }}
  a {{ color: var(--field); text-underline-offset: 3px; }}
  a:focus-visible {{ outline: 2px solid var(--field); outline-offset: 3px; }}
</style>

<div class="wrap">

  <header>
    <p class="eyebrow">Imagery detail &middot; Sentinel-2 &middot; Suffolk, 52.12&deg;N 1.40&deg;E</p>
    <h1>Does z14 buy anything?</h1>
    <p class="lede">You asked for more detail and I argued against it on the
      numbers. The numbers were right and the conclusion was wrong. Here is the
      same patch of Suffolk lane country at each level, so you can see it
      rather than take my word for it.</p>
  </header>

  <section>
    <h2>The whole tile</h2>
    <p class="sub">About 5&nbsp;km across, which is roughly what you see when
      the map is zoomed out to plan. At this scale the three are the same
      picture.</p>
    <div class="plates">
      <figure>
        <div class="plate-label"><span>Standard z13</span><span class="mpp">11.7 m/px</span></div>
        <div class="plate"><img src="{standard}" alt="Sentinel-2 imagery of Suffolk farmland at zoom 13, whole tile"></div>
        <figcaption>Native resolution. This is what ships today.</figcaption>
      </figure>
      <figure>
        <div class="plate-label"><span>z13 sharpened</span><span class="mpp">11.7 m/px</span></div>
        <div class="plate"><img src="{sharpened}" alt="The same tile with an unsharp mask applied"></div>
        <figcaption>An unsharp mask at build time. Free, in bytes.</figcaption>
      </figure>
      <figure>
        <div class="plate-label"><span>Detailed z14</span><span class="mpp">5.9 m/px</span></div>
        <div class="plate"><img src="{detailed}" alt="The same ground at zoom 14, whole tile"></div>
        <figcaption>Four times the tiles for the same view.</figcaption>
      </figure>
    </div>
  </section>

  <section>
    <h2>Zoomed in, which is where you noticed it</h2>
    <p class="sub">The centre quarter of the same ground, blown up &mdash; a
      rider looking at one lane rather than one county. This is the condition
      you described as very blurry, and the three are not the same picture.</p>
    <div class="plates">
      <figure>
        <div class="plate-label"><span>Standard z13</span><span class="mpp">11.7 m/px</span></div>
        <div class="plate"><img src="{zstandard}" alt="Zoom 13 imagery magnified, showing soft mushy detail"></div>
        <figcaption>Soft. Hedge lines and the village smear together.</figcaption>
      </figure>
      <figure>
        <div class="plate-label"><span>z13 sharpened</span><span class="mpp">11.7 m/px</span></div>
        <div class="plate"><img src="{zsharpened}" alt="Zoom 13 imagery magnified with sharpening, barely different"></div>
        <figcaption>Sharpening does not rescue it. Barely distinguishable
          from the plate on the left.</figcaption>
      </figure>
      <figure class="pick">
        <div class="plate-label"><span>Detailed z14</span><span class="mpp">5.9 m/px</span></div>
        <div class="plate"><img src="{zdetailed}" alt="Zoom 14 imagery magnified, visibly crisper field boundaries"></div>
        <figcaption>Field boundaries, the wood edge and the village all hold
          their shape. This is a real difference.</figcaption>
      </figure>
    </div>
  </section>

  <div class="verdict">
    <h2>Where I was wrong</h2>
    <p>Sentinel-2 really is 10&nbsp;m per pixel, and z13 really is that
      resolution, so z14 adds no <em>optical</em> detail. That much was right,
      and it is why the first two plates above are identical.</p>
    <p>But a rider does not look at optical resolution, they look at rendered
      pixels. At z13 the phone takes a 256-pixel JPEG and stretches it four
      times, amplifying the compression along with everything else. At z14 it
      is handed twice as many real pixels, resampled from the source mosaic by
      a proper offline resampler instead of the GPU. <strong>Same information,
      visibly better rendering &mdash; and rendering is what you were looking
      at when you called it blurry.</strong></p>
    <p>Sharpening was my cheaper suggestion. The middle plate in the second row
      is what it actually buys at zoom, which is close to nothing.</p>
  </div>

  <section>
    <h2>What it costs</h2>
    <p class="sub">For East Anglia, the area published today.</p>
    <div class="scroll">
      <table>
        <thead>
          <tr><th>Level</th><th>Tiles to fetch</th><th>Pack size</th><th>Build time, politely</th><th>Better at zoom</th></tr>
        </thead>
        <tbody>
          <tr>
            <td>Standard z13</td>
            <td class="num">~6,900</td>
            <td class="num">54 MB</td>
            <td class="num">built</td>
            <td class="no">&mdash;</td>
          </tr>
          <tr>
            <td>z13 sharpened</td>
            <td class="num">~6,900</td>
            <td class="num">54 MB</td>
            <td class="num">~1 day</td>
            <td class="no">barely</td>
          </tr>
          <tr>
            <td>Detailed z14</td>
            <td class="num">~27,000</td>
            <td class="num">~215 MB</td>
            <td class="num">~5 days</td>
            <td class="yes">yes, clearly</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p>The build is deliberately slow. EOX run the tile service free and the
      CC&nbsp;BY licence covers the data, not a right to hammer it &mdash; so it
      fetches a few thousand tiles a day into staging that survives between
      runs, and packages nothing until every tile is present.</p>
  </section>

  <section>
    <h2>The ceiling this does not lift</h2>
    <p class="sub">Worth keeping in view before anyone asks for z15.</p>
    <p>z14 is already twice the pixels the source has. z15 would be four times,
      and it is mush &mdash; there is no more information in the mosaic to
      render. Past this point, more detail needs a sharper <em>source</em>, and
      that is a licensing question rather than a technical one.</p>
    <p>The national sub-metre aerial, APGB, is public-sector internal use only
      and cannot ship in an app. The Environment Agency's vertical aerial
      photography is 10&ndash;50&nbsp;cm under the Open Government Licence and
      genuinely can &mdash; twenty to a hundred times sharper than anything on
      this page. It is flown project by project over England only, so it is
      patchy rather than national, and it is a separate pipeline.</p>
  </section>

  <footer>
    <p>Plates cut from Sentinel-2 cloudless 2024 by EOX IT Services GmbH,
      CC&nbsp;BY&nbsp;4.0. Contains modified Copernicus Sentinel data 2024.
      Every plate is the same ground: z13 tile 4127/2701, and the four z14
      tiles beneath it. Five tile requests in total.</p>
  </footer>

</div>
"""


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    html = PAGE.format(
        standard=uri("standard"),
        sharpened=uri("standard-sharpened"),
        detailed=uri("detailed"),
        zstandard=uri("zoom-standard"),
        zsharpened=uri("zoom-sharpened"),
        zdetailed=uri("zoom-detailed"),
    )
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"{OUT}  {os.path.getsize(OUT) / 1024:.0f} KB")


if __name__ == "__main__":
    main()

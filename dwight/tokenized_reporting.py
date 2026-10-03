"""Private, offline research charts. No browser requests or execution controls."""
from __future__ import annotations

from datetime import datetime
from html import escape
import json


def render_comparison(report):
    """Render the comparator's validated result without loading external assets."""
    def number(value, suffix="", places=2):
        return "Unknown" if value is None else f"{value:,.{places}f}{suffix}"

    def display_time(value):
        return datetime.fromisoformat(value).strftime('%d %b %Y, %H:%M UTC') if value else 'Time unavailable'

    def table(headers, rows):
        return '<div class="scroll"><table><thead><tr>' + ''.join(
            f'<th>{escape(str(item))}</th>' for item in headers) + '</tr></thead><tbody>' + ''.join(
            '<tr>' + ''.join(f'<td>{escape(str(item))}</td>' for item in row) + '</tr>'
            for row in rows) + '</tbody></table></div>'

    def chart(title, columns, labels, colors, suffix):
        series = [dict(row) for row in report['aligned_series']]
        for row in series:
            for column in columns:
                if column.endswith('_vwap') and not row.get(column+'_prefix_complete',False):
                    row[column] = None
        if not series:
            return f'<section><h2>{escape(title)}</h2><p>No overlapping completed bars</p></section>'
        values = [row[column] for row in series for column in columns if row.get(column) is not None]
        if not values:
            return f'<section><h2>{escape(title)}</h2><p>No comparable values</p></section>'
        low, high = min(values), max(values)
        pad = max((high-low)*.12, max(abs(high), abs(low), 1)*.00005)
        low, high = low-pad, high+pad
        x = lambda i: 65 + 895*i/max(1, len(series)-1)
        y = lambda value: 220-180*(value-low)/(high-low)
        grid = ''.join(f'<line x1="65" x2="960" y1="{40+i*45}" y2="{40+i*45}" class="grid"/><text x="55" y="{44+i*45}" text-anchor="end">{number(high-(high-low)*i/4, suffix)}</text>' for i in range(5))
        paths = []
        # Break at missing intervals rather than draw across closures or gaps.
        for column, color in zip(columns, colors):
            segments, segment, previous = [], [], None
            for i, row in enumerate(series):
                value = row.get(column)
                if value is None or (previous is not None and row['timestamp'] != previous):
                    if segment:
                        segments.append(segment)
                    segment = []
                if value is not None:
                    segment.append(f'{x(i):.2f},{y(value):.2f}')
                previous = row['bar_end']
            if segment:
                segments.append(segment)
            paths.extend(f'<polyline points="{" ".join(points)}" stroke="{color}"/>' for points in segments)
            if len(series) == 1 and series[0].get(column) is not None:
                paths.append(f'<circle cx="{x(0):.2f}" cy="{y(series[0][column]):.2f}" r="3" fill="{color}"/>')
        dates = f'<text x="65" y="250">{escape(series[0]["timestamp"][5:16])} UTC</text><text x="960" y="250" text-anchor="end">{escape(series[-1]["timestamp"][5:16])} UTC</text>'
        legend = ''.join(f'<span><i style="background:{color}"></i>{escape(label)}</span>' for label,color in zip(labels,colors))
        # JSON is data only; closing script tags and angle brackets are escaped.
        payload = json.dumps({'rows': [{key: row[key] for key in ['timestamp', *columns]} for row in series],
                              'columns': columns, 'labels': labels, 'suffix': suffix}, allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
        return f'<section class="chart"><h2>{escape(title)}</h2><div class="legend">{legend}</div><svg viewBox="0 0 1000 270" role="img" aria-label="{escape(title)}">{grid}{"".join(paths)}{dates}<line class="cursor" x1="65" x2="65" y1="30" y2="230" visibility="hidden"/></svg><input aria-label="Inspect {escape(title)}" type="range" min="0" max="{len(series)-1}" value="0"><output>Move over the chart or use the slider to inspect a bar</output><script type="application/json" class="chart-data">{payload}</script></section>'

    coverage, divergence, book = report['coverage'], report['divergence'], report['orderbook']
    synthetic = report['data_kind'] == 'synthetic'
    badge = 'Synthetic fixture' if synthetic else 'Observed markets · private research'
    cards = ''.join(f'<div class="card"><small>{escape(label)}</small><strong>{escape(str(value))}</strong></div>' for label,value in [
        ('Matched five-minute bars', coverage['common_bar_count']),
        ('Bars with positive volume', coverage.get('common_active_bar_count','Unknown')),
        ('Active-bar absolute difference', number(divergence.get('active_bar_mean_absolute_bps'), ' bps')),
        ('QQQx book spread', number(book.get('spread_bps'), ' bps'))])
    book_rows = []
    for side, key in [('Buy','buy_sweeps'),('Sell','sell_sweeps')]:
        for sweep in book.get(key, []):
            book_rows.append([side, sweep['requested_units'], sweep['filled_units'],
                              'Complete depth' if sweep['complete'] else 'Insufficient depth',
                              number(sweep.get('full_fill_average_price_usd'), ' USD'),
                              number(sweep.get('slippage_vs_mid_bps'), ' bps')])
    depth = table(['Side','QQQx units requested','Units visible','Depth coverage','Full-size average','Difference from mid'],book_rows) if book_rows else '<p>No usable order-book snapshot</p>'
    coverage_table = table(['Evidence','Bars'], [
        ['QQQ regular-session bars', coverage['qqq_complete_regular_bars']],
        ['QQQx regular-session bars', coverage['token_complete_regular_bars']],
        ['Matched QQQx bars with zero volume', coverage.get('common_zero_volume_token_bars','Unknown')],
        ['QQQ bars missing QQQx', coverage['missing_token_for_qqq']],
        ['QQQx regular bars missing QQQ', coverage['missing_qqq_for_token_regular']],
        ['QQQx bars outside regular sessions', coverage['token_bars_outside_regular_sessions']]])
    source_rows = []
    for label, source in [('QQQ',report['sources']['qqq']),('QQQx',report['sources']['qqqx'])]:
        identity = source.get('instrument',source)
        source_rows.append([label,source.get('provider',identity.get('venue','unknown')),
                            source.get('feed',identity.get('pair','unknown')),
                            source.get('adjustment','provider reported'),
                            source.get('collected_at','See snapshot receipt times')])
    limitations = ''.join(f'<li>{escape(item)}</li>' for item in report['limitations'])
    price = chart('Reported five-minute closes', ['qqq_close','qqqx_close'], ['QQQ','QQQx including zero-volume marks'], ['#e9e9e9','#63cbb5'], ' USD')
    difference = chart('Indicative price difference', ['divergence_bps'], ['QQQx reported close relative to QQQ close'], ['#63cbb5'], ' bps')
    vwap = chart('Regular-session bar VWAP', ['qqq_vwap','qqqx_vwap'], ['QQQ source volume','QQQx venue volume'], ['#e9e9e9','#63cbb5'], ' USD')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>Dwight | QQQ and QQQx research</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#090909;color:#efefef;font:15px/1.55 system-ui,sans-serif}}main{{max-width:1140px;margin:auto;padding:42px 32px 70px}}header{{border-bottom:1px solid #262626;padding-bottom:26px}}.badge,small,.subtle{{color:#999}}.badge{{font-size:12px;letter-spacing:.08em;text-transform:uppercase}}h1{{font-size:clamp(30px,5vw,48px);letter-spacing:-.045em;line-height:1.1;margin:20px 0 14px}}h2{{font-size:21px;font-weight:550;letter-spacing:-.025em;margin:0 0 12px}}p{{margin:10px 0;color:#aaa}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:28px 0}}.card,section{{border:1px solid #262626;border-radius:14px;background:#101010}}.card{{padding:20px}}strong{{display:block;font-size:27px;font-weight:500;line-height:1.2;margin-top:12px}}section{{padding:24px;margin-top:18px}}.legend{{display:flex;gap:24px;flex-wrap:wrap;font-size:12px;color:#aaa}}.legend i{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px}}svg{{display:block;width:100%;margin-top:12px;overflow:visible}}svg text{{font-size:11px;fill:#888}}svg polyline{{stroke-width:2;fill:none;stroke-linejoin:round}}.grid{{stroke:#282828;stroke-width:1}}.cursor{{stroke:#888;stroke-dasharray:3 3}}input[type=range]{{display:block;width:100%;accent-color:#63cbb5;margin:12px 0}}output{{display:block;color:#aaa;min-height:42px;font-size:12px;overflow-wrap:anywhere}}.scroll{{overflow:auto}}table{{width:100%;border-collapse:collapse;font-size:12px}}th,td{{padding:13px 12px;text-align:left;border-bottom:1px solid #262626;white-space:nowrap}}th{{font-weight:400;color:#999}}details{{margin-top:20px}}summary{{cursor:pointer;color:#ccc}}li{{color:#999;margin:9px 0}}a{{color:#ccc}}@media(max-width:700px){{main{{padding:26px 16px 45px}}.cards{{grid-template-columns:repeat(2,1fr)}}.card{{padding:14px}}strong{{font-size:21px}}section{{padding:16px}}svg text{{font-size:11px}}}}
</style></head><body><main><header><div class="badge">Dwight · {badge}</div><h1>QQQ and QQQx</h1><p>Compare two markets on matching completed bars</p><p class="subtle">Aligned-bar cutoff: {escape(display_time(report['as_of']))}. No orders or strategy returns.</p></header><div class="cards">{cards}</div><p>QQQx zero-volume intervals may carry earlier prices. All reported closes: average absolute difference {number(divergence['mean_absolute_bps'], ' bps')}. Neither comparison establishes executable liquidity.</p>{price}{difference}{vwap}<section><h2>QQQx order-book snapshot</h2><p>{escape(display_time(book.get('observed_at')))} · {escape(book['status'])} at the frozen observation</p>{depth}<p>Visible depth is a hypothetical sweep without fees, latency or fills. QQQx units have no verified share conversion. The QQQ dataset has no executable quotes.</p></section><section><h2>Observation coverage</h2>{coverage_table}<p>Missing reference prices stay unknown. Charts break across closures and gaps.</p></section><details><summary>Sources and method</summary>{table(['Asset','Provider','Feed or pair','Adjustment','Collected'],source_rows)}<ul>{limitations}</ul><p>Private market data stays outside public deployment bundles. Local hashes verify file integrity, not account eligibility or economic rights.</p></details><p class="subtle">Research only · no trained model · no account connected</p></main><script>
document.querySelectorAll('.chart').forEach(panel=>{{
const data=JSON.parse(panel.querySelector('.chart-data').textContent),svg=panel.querySelector('svg'),slider=panel.querySelector('input'),cursor=panel.querySelector('.cursor'),out=panel.querySelector('output');
const geometry=Array.from(svg.querySelectorAll('polyline,line,text,circle')).filter(el=>el!==cursor).map(el=>[el,Object.fromEntries(['points','x','x1','x2','cx'].filter(key=>el.hasAttribute(key)).map(key=>[key,el.getAttribute(key)]))]);let width=1000;
function xpos(index){{return 65+(width-105)*index/Math.max(1,data.rows.length-1)}}
function show(index){{index=Math.max(0,Math.min(data.rows.length-1,index));slider.value=index;let row=data.rows[index];out.textContent=row.timestamp+' · '+data.columns.map((key,i)=>data.labels[i]+': '+(row[key]===null?'Unknown':Number(row[key]).toFixed(3)+data.suffix)).join(' · ');cursor.setAttribute('x1',xpos(index));cursor.setAttribute('x2',xpos(index));cursor.setAttribute('visibility','visible')}}
function layout(){{width=Math.max(310,svg.clientWidth);svg.setAttribute('viewBox','0 0 '+width+' 270');const transform=x=>Number(x)<=65?Number(x):65+(Number(x)-65)/895*(width-105);geometry.forEach(([el,attrs])=>Object.entries(attrs).forEach(([key,value])=>el.setAttribute(key,key==='points'?value.split(' ').map(pair=>{{const [x,y]=pair.split(',');return transform(x)+','+y}}).join(' '):transform(value))));if(cursor.getAttribute('visibility')==='visible')show(Number(slider.value))}}
slider.addEventListener('input',()=>show(Number(slider.value)));svg.addEventListener('pointermove',event=>{{const bounds=svg.getBoundingClientRect();show(Math.round((((event.clientX-bounds.left)/bounds.width)*width-65)/(width-105)*(data.rows.length-1)))}});layout();window.addEventListener('resize',layout);
}});
</script></body></html>'''

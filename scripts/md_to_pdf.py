#!/usr/bin/env python3
"""
SMPD ALPR Findings — Markdown-to-PDF generator.
Reads .md file at runtime. Zero hardcoded bullet text.
"""

import re, sys, tempfile
from pathlib import Path
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.platypus import (
    Paragraph, Spacer, PageBreak, Table, TableStyle,
    HRFlowable, Flowable, Frame, PageTemplate, BaseDocTemplate, KeepTogether
)
from reportlab.platypus.doctemplate import NextPageTemplate
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing

DARK_BLUE  = HexColor("#1a2744")
MED_BLUE   = HexColor("#2c4a7c")
ACCENT_BLUE= HexColor("#3b6cb4")
LIGHT_GRAY = HexColor("#f4f5f7")
MED_GRAY   = HexColor("#e0e2e6")
TEXT_COLOR  = HexColor("#1f2937")
MUTED      = HexColor("#6b7280")
RED_ACCENT = HexColor("#b91c1c")
LINK_BLUE  = "#1e40af"
PAGE_W, PAGE_H = letter
ML, MR, MT, MB = 0.72*inch, 0.72*inch, 0.72*inch, 0.72*inch
COUNCIL_PORTAL = "https://www.cityofsanmateo.org/publicmeetings"

class BookmarkAnchor(Flowable):
    def __init__(self, name):
        super().__init__(); self.name=name; self.width=self.height=0
    def wrap(self,aW,aH): return (0,0)
    def draw(self): self.canv.bookmarkHorizontal(self.name, 0, 14)

class QRFlowable(Flowable):
    def __init__(self, url, size=44):
        super().__init__(); self.url=url; self.size=size; self.width=self.height=size
    def wrap(self,aW,aH): return (self.size, self.size)
    def draw(self):
        qr=QrCodeWidget(self.url); b=qr.getBounds()
        d=Drawing(self.size,self.size,transform=[self.size/(b[2]-b[0]),0,0,self.size/(b[3]-b[1]),0,0])
        d.add(qr); d.drawOn(self.canv,0,0)
        self.canv.linkURL(self.url, (0, 0, self.size, self.size), relative=1)

_MD5_HASH = ""  # set at build time

def on_cover(c,doc): pass
def on_content(canvas,doc):
    canvas.saveState()
    canvas.setStrokeColor(MED_GRAY); canvas.setLineWidth(0.5)
    canvas.line(ML, PAGE_H-0.6*inch, PAGE_W-MR, PAGE_H-0.6*inch)
    canvas.setFont("Helvetica",8); canvas.setFillColor(MUTED)
    if _MD5_HASH:
        canvas.setFont("Helvetica",6.5)
        canvas.drawString(ML, 0.5*inch, f"Version: {_MD5_HASH[:12]}")
    canvas.setFont("Helvetica",8)
    canvas.drawRightString(PAGE_W-MR, 0.5*inch, f"Page {doc.page}")
    canvas.restoreState()

def mkstyles():
    ss=getSampleStyleSheet(); s={}
    s['cover_title']=ParagraphStyle('ct',parent=ss['Title'],fontName='Helvetica-Bold',fontSize=26,leading=32,textColor=DARK_BLUE,alignment=TA_LEFT,spaceAfter=6)
    s['cover_sub']=ParagraphStyle('cs',parent=ss['Normal'],fontName='Helvetica',fontSize=13,leading=18,textColor=MUTED,alignment=TA_LEFT,spaceAfter=4)
    s['cover_note']=ParagraphStyle('cn',parent=ss['Normal'],fontName='Helvetica',fontSize=10,leading=14,textColor=TEXT_COLOR,alignment=TA_LEFT,spaceBefore=18,spaceAfter=6)
    s['toc_hdr']=ParagraphStyle('th2',parent=ss['Heading1'],fontName='Helvetica-Bold',fontSize=16,leading=20,textColor=DARK_BLUE,spaceBefore=0,spaceAfter=12)
    s['toc_entry']=ParagraphStyle('te',parent=ss['Normal'],fontName='Helvetica',fontSize=11,leading=18,textColor=TEXT_COLOR,leftIndent=12)
    s['sec_head']=ParagraphStyle('sh',parent=ss['Heading1'],fontName='Helvetica-Bold',fontSize=16,leading=20,textColor=DARK_BLUE,spaceBefore=22,spaceAfter=4)
    s['tldr']=ParagraphStyle('tldr',parent=ss['Normal'],fontName='Helvetica-BoldOblique',fontSize=10,leading=14,textColor=MED_BLUE,spaceBefore=2,spaceAfter=10,backColor=HexColor("#eef2f9"),borderPadding=(6,8,6,8))
    s['body']=ParagraphStyle('body',parent=ss['Normal'],fontName='Helvetica',fontSize=10,leading=14.5,textColor=TEXT_COLOR,alignment=TA_JUSTIFY,spaceBefore=2,spaceAfter=2)
    s['bullet']=ParagraphStyle('bl',parent=s['body'],leftIndent=12,firstLineIndent=0,spaceBefore=5,spaceAfter=5,bulletIndent=0,bulletFontSize=10)
    s['src_ref']=ParagraphStyle('sr',parent=ss['Normal'],fontName='Helvetica',fontSize=8.5,leading=11,textColor=MUTED)
    s['th']=ParagraphStyle('thd',parent=ss['Normal'],fontName='Helvetica-Bold',fontSize=9,leading=12,textColor=white,alignment=TA_LEFT)
    s['tc']=ParagraphStyle('tc',parent=ss['Normal'],fontName='Helvetica',fontSize=8.5,leading=11.5,textColor=TEXT_COLOR,alignment=TA_LEFT)
    s['tcs']=ParagraphStyle('tcs',parent=ss['Normal'],fontName='Helvetica',fontSize=7.5,leading=10,textColor=TEXT_COLOR,alignment=TA_LEFT)
    s['contact']=ParagraphStyle('co',parent=s['body'],fontSize=10,leading=15,spaceBefore=2,spaceAfter=2,leftIndent=18)
    s['verify']=ParagraphStyle('vi',parent=s['body'],fontSize=9.5,leading=13.5,spaceBefore=3,spaceAfter=3,leftIndent=18)
    s['exec_body']=ParagraphStyle('eb',parent=s['body'],fontSize=10.5,leading=15.5,spaceBefore=4,spaceAfter=8)
    s['kf_item']=ParagraphStyle('kf',parent=s['body'],fontSize=10,leading=14.5,spaceBefore=4,spaceAfter=4,leftIndent=24,firstLineIndent=0,bulletIndent=8)
    s['kf_hdr']=ParagraphStyle('kfh',parent=s['sec_head'],fontSize=13,leading=17,spaceBefore=14,spaceAfter=6)
    s['qr_lbl']=ParagraphStyle('ql',parent=ss['Normal'],fontName='Helvetica',fontSize=7,leading=9,textColor=TEXT_COLOR,alignment=TA_CENTER)
    s['app_head']=ParagraphStyle('ah',parent=s['sec_head'],fontSize=14,leading=18,spaceBefore=16,spaceAfter=4)
    s['app_sub']=ParagraphStyle('as2',parent=ss['Heading2'],fontName='Helvetica-Bold',fontSize=11,leading=14,textColor=MED_BLUE,spaceBefore=10,spaceAfter=4)
    s['app_subsub']=ParagraphStyle('as3',parent=ss['Heading3'],fontName='Helvetica-Bold',fontSize=10,leading=13,textColor=DARK_BLUE,spaceBefore=8,spaceAfter=3)
    s['app_body']=ParagraphStyle('ab',parent=s['body'],fontSize=9.5,leading=13.5,spaceBefore=2,spaceAfter=4)
    s['app_bullet']=ParagraphStyle('abl',parent=s['app_body'],leftIndent=18,bulletIndent=4,spaceBefore=2,spaceAfter=2)
    s['app_list']=ParagraphStyle('ali',parent=s['app_body'],leftIndent=18,spaceBefore=1,spaceAfter=1)
    return s

def ilink(t,bm,color=LINK_BLUE): return f'<a href="#{bm}" color="{color}">{t}</a>'
def elink(t,url,color=LINK_BLUE): return f'<a href="{url}" color="{color}">{t}</a>'
_SEC_ANCHORS = {}  # populated by parse_md(), keyed by section number string

def _slugify(text):
    """Convert heading text to a URL-safe anchor slug."""
    text = re.sub(r'^\d+\.\s*', '', text)  # strip leading number
    text = re.sub(r'[^a-z0-9\s]', '', text.lower())
    return 'sec_' + re.sub(r'\s+', '_', text.strip())

def md_to_xml(text, exec_mode=False):
    rv = RED_ACCENT.hexval()
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*([^*]+?)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', lambda m: elink(m.group(1),m.group(2)), text)
    if exec_mode:
        text = re.sub(r'\(see (§[\d,\s§–]+)\)', _resolve_section_refs, text)
        text = re.sub(r'\(See ([^)]*§[^)]+)\)', _resolve_section_refs, text)
    text = re.sub(r'\[(\d{1,2}(?:[^\]\d][^\]]*)?)\]', lambda m: f'<a href="#source_{re.findall(r"[0-9]+",m.group(1))[0]}" color="{LINK_BLUE}">{m.group(0)}</a>' if re.findall(r"[0-9]+",m.group(1)) else m.group(0), text)
    text = re.sub(r'\[PRA not yet filed\]', f'<font color="{rv}"><i>[PRA not yet filed]</i></font>', text)
    text = re.sub(r'\[VERIFY[^\]]*\]', lambda m: f'<font color="{rv}"><i>{m.group(0)}</i></font>', text)
    text = text.replace('\u2705', '<font name="ZapfDingbats" color="#15803d" size="10">&#x2714;</font>')
    text = text.replace('\u274c', '<font name="ZapfDingbats" color="#b91c1c" size="10">&#x2718;</font>')
    text = text.replace('\u26a0\ufe0f', '<font color="#d97706"><b>&#x25b2;</b></font>')
    text = text.replace('\u26a0', '<font color="#d97706"><b>&#x25b2;</b></font>')
    return text

def _resolve_section_refs(m):
    """Turn '(see §1, §2)' or '(See Appendix B; see §1–7.)' into linked text."""
    def _link_one(sm):
        num = sm.group(1)
        anchor = _SEC_ANCHORS.get(num, f'sec_{num}')
        return f'<a href="#{anchor}" color="{LINK_BLUE}">\xa7{num}</a>'
    return re.sub(r'§(\d+)', _link_one, m.group(0))

def _back(styles):
    return Paragraph(ilink("\u25b2 Contents","toc",color=MUTED.hexval()), styles['src_ref'])

def extract_urls(md_link_text):
    """Extract URLs from markdown links only — no bare URL duplication."""
    urls = re.findall(r'\[([^\]]+)\]\((https?://[^\)]+)\)', md_link_text)
    seen = set(); result = []
    for label, url in urls:
        if url not in seen: result.append((label, url)); seen.add(url)
    return result

COUNCIL_FILE_IDS = {'5':'23-7622','6':'24-8392','7':'20-3547','8':'23-7622','9':'24-8392'}
COUNCIL_SOURCE_NUMS = set(COUNCIL_FILE_IDS.keys())

# ═══════════════════ PARSER ═══════════════════

def parse_md(path):
    """Split markdown into sections by headings.  Returns a list of
    {'title': str, 'anchor': str, 'lines': [str]} dicts,
    plus a preamble (lines before the first heading)."""
    global _SEC_ANCHORS
    with open(path, 'r') as f: raw_lines = f.readlines()
    # Strip HTML comments and --- separators
    lines = []
    in_comment = False
    for line in raw_lines:
        s = line.rstrip('\n')
        if '<!--' in s:
            if '-->' in s: continue
            in_comment = True; continue
        if in_comment:
            if '-->' in s: in_comment = False
            continue
        if s.strip() == '---': continue
        lines.append(s)
    # Split into blocks by heading
    blocks = []  # list of {'title','anchor','lines'}
    preamble = []
    for line in lines:
        stripped = line.strip()
        m = re.match(r'^(##)\s+(.+)', stripped)  # only ## splits; ### stays as content
        if m:
            title = m.group(2).strip()
            nm = re.match(r'^(\d+)\.\s+(.+)', title)
            anchor = _slugify(nm.group(2)) if nm else _slugify(title)
            blocks.append({'title': title, 'anchor': anchor, 'lines': []})
        elif blocks:
            blocks[-1]['lines'].append(line)
        else:
            preamble.append(stripped)
    # Build the doc dict that builders expect, interpreting blocks generically
    doc = dict(preamble_lines=preamble, exec_paras=[], key_findings=[], sections=[],
               source_intro='', source_rows=[], contacts=[],
               appendix_a_lines=[], appendix_b_lines=[], verify_items=[])
    for block in blocks:
        title_low = block['title'].lower()
        nm = re.match(r'^(\d+)\.\s+(.+)', block['title'])
        if nm:
            # Numbered section — extract tables + bullets
            sec = {'num': nm.group(1), 'title': nm.group(2), 'anchor': block['anchor'],
                   'table_rows': [], 'bullets': []}
            for line in block['lines']:
                raw = line; stripped = line.strip()
                if not stripped: continue
                if stripped.startswith('|'):
                    cleaned = stripped.replace('-', '').replace('|', '').replace(' ', '').replace(':', '')
                    if cleaned == '': continue
                    parts = [p.strip() for p in stripped.split('|')[1:-1]]
                    if parts: sec['table_rows'].append(parts)
                elif re.match(r'^  +- ', raw):
                    sub_text = re.sub(r'^  +- ', '', raw)
                    if sec['bullets']:
                        last = sec['bullets'][-1]
                        if isinstance(last, dict):
                            last['subs'].append(sub_text)
                        else:
                            sec['bullets'][-1] = {'text': last, 'subs': [sub_text]}
                    else:
                        sec['bullets'].append(sub_text)
                elif stripped.startswith('- '):
                    sec['bullets'].append(stripped[2:])
            doc['sections'].append(sec)
        elif title_low.startswith('executive summary'):
            doc['exec_paras'] = [s.strip() for s in block['lines']
                                 if s.strip() and not s.strip().startswith('#')]
        elif title_low.startswith('key findings'):
            doc['key_findings'] = [s.strip() for s in block['lines']
                                   if re.match(r'^\d+\. ', s.strip())]
        elif title_low.startswith('source documents'):
            for s in block['lines']:
                s = s.strip()
                if s.startswith('**How to look up'): doc['source_intro'] = s
                elif s.startswith('|') and not s.startswith('|---') and not s.startswith('| #'):
                    parts = [p.strip() for p in s.split('|')[1:-1]]
                    if len(parts) >= 3: doc['source_rows'].append((parts[0], parts[1], parts[2]))
        elif title_low.startswith('key contacts'):
            doc['contacts'] = [s.strip()[2:] for s in block['lines'] if s.strip().startswith('- ')]
        elif title_low.startswith('appendix a'):
            doc['appendix_a_title'] = block['title']
            doc['appendix_a_lines'] = block['lines']
        elif title_low.startswith('appendix b'):
            doc['appendix_b_title'] = block['title']
            doc['appendix_b_lines'] = block['lines']
        elif 'items requiring verification' in title_low:
            doc['verify_items'] = [s.strip()[2:] for s in block['lines'] if s.strip().startswith('- ')]
        # Sub-headings (### within a ## block) and unrecognized headings are
        # already captured in their parent block's lines by the splitter above,
        # since only the first heading creates a new block.  No action needed.
    _SEC_ANCHORS = {sec['num']: sec['anchor'] for sec in doc['sections']}
    return doc

# ═══════════════════ BUILDERS ═══════════════════

def build_cover(S, dd):
    note=''; date_lines=[]
    for l in dd['preamble_lines']:
        if l.startswith('*') and l.endswith('*'):
            date_lines.append(l.strip('*').strip())
        elif l and not l.startswith('*') and not l.startswith('#'):
            note = l
    els = [Spacer(1,1.5*inch),Paragraph("SMPD ALPR Investigation",S['cover_title']),
            Paragraph("Findings",S['cover_title']),Spacer(1,8),
            HRFlowable(width="40%",thickness=2,color=ACCENT_BLUE,spaceAfter=14,spaceBefore=6,hAlign='LEFT')]
    for date_line in date_lines:
        els.append(Paragraph(date_line,S['cover_sub']))
    els.append(Spacer(1,24))
    if note:
        els.append(Paragraph(md_to_xml(note),S['cover_note']))
    els.append(PageBreak())
    return els

def build_toc(S, dd):
    els=[BookmarkAnchor("toc"),Paragraph("Contents",S['toc_hdr']),Spacer(1,4)]
    els.append(Paragraph(ilink("<u>Executive Summary</u>","exec_summary",LINK_BLUE),S['toc_entry']))
    for sec in dd['sections']:
        els.append(Paragraph(ilink(f"<u>{sec['num']}. {sec['title']}</u>",sec['anchor'],LINK_BLUE),S['toc_entry']))
    els.append(Spacer(1,6))
    extras=[("Source Documents","source_docs"),("Key Contacts","contacts"),
            ("Appendix A: Agency Access Breakdown","appendix_a")]
    if dd.get('appendix_b_lines'): extras.append(("Appendix B: Statutory Gap Analysis","appendix_b"))
    if dd.get('verify_items'): extras.append(("Items Requiring Verification","verify"))
    for label,anchor in extras:
        els.append(Paragraph(ilink(f"<u>{label}</u>",anchor,LINK_BLUE),S['toc_entry']))
    els+=[Spacer(1,12),HRFlowable(width="100%",thickness=0.5,color=MED_GRAY,spaceAfter=6),PageBreak()]
    return els

def build_exec(S, dd):
    els=[BookmarkAnchor("exec_summary"),_back(S),Paragraph("Executive Summary",S['sec_head'])]
    for p in dd['exec_paras']: els.append(Paragraph(md_to_xml(p),S['exec_body']))
    kf_header = Paragraph("Key Findings",S['kf_hdr'])
    kf_items = []
    for f in dd['key_findings']:
        m=re.match(r'^(\d+)\. (.+)',f)
        if m: kf_items.append(Paragraph(md_to_xml(m.group(2),exec_mode=True),S['kf_item'],bulletText=f'{m.group(1)}.'))
    if kf_items:
        els.append(KeepTogether([kf_header, kf_items[0]]))
        for item in kf_items[1:]:
            els.append(KeepTogether([item]))
    els.append(HRFlowable(width="100%",thickness=0.5,color=MED_GRAY,spaceAfter=4,spaceBefore=12))
    return els

def _build_section_table(rows, S):
    """Build a section-level compliance/comparison table."""
    if not rows: return None
    ncols = max(len(r) for r in rows)
    data = []
    for i, parts in enumerate(rows):
        style = S['th'] if i == 0 else S['tcs']
        cells = [Paragraph(md_to_xml(p), style) for p in parts]
        while len(cells) < ncols: cells.append(Paragraph("", style))
        data.append(cells)
    avail = PAGE_W - ML - MR
    if ncols == 3:
        cw = [1.3*inch, 2.1*inch, avail - 3.4*inch]
    elif ncols == 4:
        cw = [1.2*inch] + [(avail - 1.2*inch) / 3] * 3
    elif ncols == 5:
        cw = [1.1*inch] + [(avail - 1.1*inch) / 4] * 4
    elif ncols == 6:
        cw = [1.0*inch] + [(avail - 1.0*inch) / 5] * 5
    elif ncols >= 7:
        cw = [0.95*inch] + [(avail - 0.95*inch) / (ncols - 1)] * (ncols - 1)
    else:
        cw = [avail / ncols] * ncols
    t = Table(data, colWidths=cw, repeatRows=1)
    cmds = [('BACKGROUND',(0,0),(-1,0),DARK_BLUE),('TEXTCOLOR',(0,0),(-1,0),white),
            ('GRID',(0,0),(-1,-1),0.4,MED_GRAY),('VALIGN',(0,0),(-1,-1),'TOP'),
            ('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),3),
            ('LEFTPADDING',(0,0),(-1,-1),3),('RIGHTPADDING',(0,0),(-1,-1),3),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[white,LIGHT_GRAY])]
    t.setStyle(TableStyle(cmds))
    return t

def build_section(S, sec):
    els = []
    hdr = [BookmarkAnchor(sec['anchor']), _back(S),
           Paragraph(f"{sec['num']}. {sec['title']}", S['sec_head'])]
    tbl = _build_section_table(sec.get('table_rows', []), S)
    if tbl:
        hdr.append(Spacer(1, 6))
        hdr.append(tbl)
        hdr.append(Spacer(1, 8))
    els.append(KeepTogether(hdr))
    sub_style = ParagraphStyle('sub_bullet', parent=S['bullet'],
                               leftIndent=28, bulletIndent=16, fontSize=9.5, leading=13.5)
    for b in sec['bullets']:
        if isinstance(b, dict):
            group = [Paragraph(md_to_xml(b['text']), S['bullet'], bulletText='\u2022')]
            for sub in b['subs']:
                group.append(Paragraph(md_to_xml(sub), sub_style, bulletText='\u00b7'))
            els.append(KeepTogether(group))
        else:
            els.append(KeepTogether([Paragraph(md_to_xml(b), S['bullet'], bulletText='\u2022')]))
    return els

def _make_qr_cell(urls, S):
    if not urls: return Paragraph("\u2014",S['tcs'])
    QR_SZ=44; items=[]
    for label,url in urls[:3]:
        # Use parenthetical content as label if present, otherwise truncate
        paren = re.search(r'\(([^)]+)\)', label)
        short_label = paren.group(1)[:22] if paren else label[:22]
        qr=QRFlowable(url,size=QR_SZ); lbl=Paragraph(f'<font size="5.5">{short_label}</font>',S['qr_lbl'])
        mini=Table([[qr],[lbl]],colWidths=[QR_SZ+6])
        mini.setStyle(TableStyle([('ALIGN',(0,0),(-1,-1),'CENTER'),('TOPPADDING',(0,0),(-1,-1),1),('BOTTOMPADDING',(0,0),(-1,-1),1),('LEFTPADDING',(0,0),(-1,-1),1),('RIGHTPADDING',(0,0),(-1,-1),1)]))
        items.append(mini)
    rows=[]
    for j in range(0,len(items),2):
        row=items[j:j+2]
        while len(row)<2: row.append("")
        rows.append(row)
    grid=Table(rows,colWidths=[QR_SZ+10]*2)
    grid.setStyle(TableStyle([('ALIGN',(0,0),(-1,-1),'CENTER'),('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),1),('BOTTOMPADDING',(0,0),(-1,-1),1)]))
    return grid

def _make_council_qr(file_id, S):
    QR_SZ=44; qr=QRFlowable(COUNCIL_PORTAL,size=QR_SZ)
    lbl=Paragraph(f'<font size="6.5">Adv. Search \u2192<br/>Tracking #:<br/><b>{file_id}</b></font>',
        ParagraphStyle('cql',parent=S['tcs'],alignment=TA_CENTER,fontSize=6.5,leading=8.5))
    mini=Table([[qr],[lbl]],colWidths=[QR_SZ+12])
    mini.setStyle(TableStyle([('ALIGN',(0,0),(-1,-1),'CENTER'),('TOPPADDING',(0,0),(-1,-1),1),('BOTTOMPADDING',(0,0),(-1,-1),1),('LEFTPADDING',(0,0),(-1,-1),1),('RIGHTPADDING',(0,0),(-1,-1),1)]))
    return mini

def build_source_table(S, dd):
    els=[BookmarkAnchor("source_docs"),_back(S),Paragraph("Source Documents",S['sec_head'])]
    els.append(Paragraph(md_to_xml(dd['source_intro']),S['body']))
    els.append(Spacer(1,8))
    cw=[0.28*inch,2.05*inch,2.55*inch,1.52*inch]
    hdr=[Paragraph("#",S['th']),Paragraph("Document",S['th']),Paragraph("Location / Links",S['th']),Paragraph("QR",S['th'])]
    data=[hdr]
    for num,doc_name,link_text in dd['source_rows']:
        link_xml=md_to_xml(link_text)
        if num in COUNCIL_SOURCE_NUMS:
            qr_cell=_make_council_qr(COUNCIL_FILE_IDS[num],S)
        else:
            urls=extract_urls(link_text)
            qr_cell=_make_qr_cell(urls,S) if urls else Paragraph("\u2014",S['tcs'])
        # Embed bookmark anchor in the # cell so links jump to the specific row
        num_cell = Paragraph(f'<a name="source_{num}"/>{num}',S['tc'])
        data.append([num_cell,Paragraph(doc_name,S['tc']),Paragraph(link_xml,S['tcs']),qr_cell])
    t=Table(data,colWidths=cw,repeatRows=1)
    cmds=[('BACKGROUND',(0,0),(-1,0),DARK_BLUE),('TEXTCOLOR',(0,0),(-1,0),white),
          ('GRID',(0,0),(-1,-1),0.4,MED_GRAY),('VALIGN',(0,0),(-1,-1),'TOP'),
          ('VALIGN',(3,1),(3,-1),'MIDDLE'),('ALIGN',(3,0),(3,-1),'CENTER'),
          ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),
          ('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),4)]
    for i in range(1,len(data)):
        cmds.append(('BACKGROUND',(0,i),(-1,i),white if i%2==1 else LIGHT_GRAY))
    t.setStyle(TableStyle(cmds)); els.append(t)
    return els

def build_contacts(S, dd):
    els=[BookmarkAnchor("contacts"),_back(S),Paragraph("Key Contacts",S['sec_head'])]
    for c in dd['contacts']: els.append(Paragraph(md_to_xml(c),S['contact'],bulletText='\u2022'))
    return els

def _build_table(rows, S):
    if not rows: return Spacer(1,1)
    data=[]
    for i,parts in enumerate(rows):
        style=S['th'] if i==0 else S['tc']
        data.append([Paragraph(md_to_xml(p),style) for p in parts])
    mc=max(len(r) for r in data)
    for r in data:
        while len(r)<mc: r.append(Paragraph("",S['tc']))
    avail=PAGE_W-ML-MR
    if mc==2: cw=[avail*0.5]*2
    elif mc==3: cw=[2.2*inch,0.7*inch,avail-2.9*inch]
    elif mc==4: cw=[1.3*inch,1.5*inch,1.5*inch,avail-4.3*inch]
    else: cw=[avail/mc]*mc
    t=Table(data,colWidths=cw,repeatRows=1)
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),DARK_BLUE),('TEXTCOLOR',(0,0),(-1,0),white),
        ('GRID',(0,0),(-1,-1),0.4,MED_GRAY),('VALIGN',(0,0),(-1,-1),'TOP'),
        ('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),3),
        ('LEFTPADDING',(0,0),(-1,-1),3),('RIGHTPADDING',(0,0),(-1,-1),3),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),[white,LIGHT_GRAY])]))
    return t

def _render_appendix(lines, S, anchor, title):
    header_els=[BookmarkAnchor(anchor),_back(S),Paragraph(title,S['app_head'])]
    els=[]
    pending_table=[]
    # Collect lines into subsections grouped by ### headings
    # Each subsection (### heading + content) gets KeepTogether
    subsection_buf = []  # current subsection flowables

    def flush_table():
        nonlocal pending_table
        if pending_table:
            t = _build_table(pending_table, S)
            subsection_buf.append(t)
            pending_table = []

    def flush_subsection():
        nonlocal subsection_buf
        if not subsection_buf: return
        current_group = list(subsection_buf)
        has_table = any(isinstance(item, Table) for item in current_group)
        if has_table:
            # Tables can be very tall — don't wrap in KeepTogether
            for item in current_group:
                els.append(item)
        elif len(current_group) <= 12:
            els.append(KeepTogether(current_group))
        else:
            # Keep heading + first 2 items together, rest individually
            els.append(KeepTogether(current_group[:3]))
            for item in current_group[3:]:
                els.append(item)
        subsection_buf = []

    def _make_para(stripped):
        """Convert a stripped line to a flowable."""
        if stripped.startswith('- '):
            return Paragraph(md_to_xml(stripped[2:]),S['app_list'],bulletText='\u2022')
        if stripped.startswith('**') and ':' in stripped[:60]:
            return Paragraph(md_to_xml(stripped),S['app_body'])
        if stripped.startswith('*') and stripped.endswith('*'):
            return Paragraph(md_to_xml(stripped),S['app_body'])
        return Paragraph(md_to_xml(stripped),S['app_body'])

    for line in lines:
        stripped=line.strip()
        if not stripped or stripped=='---':
            flush_table(); continue
        if stripped.startswith('|'):
            if stripped.replace('-','').replace('|','').replace(' ','')=='' or stripped.replace(':','').replace('-','').replace('|','').replace(' ','')=='': continue
            parts=[p.strip() for p in stripped.split('|')[1:-1]]
            pending_table.append(parts); continue
        else:
            flush_table()
        if stripped.startswith('## '):
            flush_subsection()
            els.append(Paragraph(md_to_xml(stripped[3:]),S['app_sub']))
            continue
        if stripped.startswith('### '):
            flush_subsection()
            subsection_buf.append(Paragraph(md_to_xml(stripped[4:]),S['app_subsub']))
            continue
        subsection_buf.append(_make_para(stripped))
    flush_table()
    flush_subsection()
    return header_els + els

def build_appendix_a(S,dd):
    return _render_appendix(dd['appendix_a_lines'],S,'appendix_a',dd.get('appendix_a_title','Appendix A'))

def build_appendix_b(S,dd):
    if not dd.get('appendix_b_lines'): return []
    return _render_appendix(dd['appendix_b_lines'],S,'appendix_b',dd.get('appendix_b_title','Appendix B'))

def build_verify(S, dd):
    els=[BookmarkAnchor("verify"),_back(S),Paragraph("Items Requiring Verification",S['sec_head'])]
    rv=RED_ACCENT.hexval(); done=MUTED.hexval()
    for item in dd['verify_items']:
        if item.startswith('[ ]'): text=f'<font color="{rv}">\u2610</font> '+item[4:]
        elif item.startswith('[x]'): text=f'<font color="{done}">\u2611</font> '+item[4:]
        else: text=item
        els.append(Paragraph(md_to_xml(text),S['verify']))
    return els

def main():
    global _MD5_HASH
    import hashlib
    md_path=str(Path(sys.argv[1]).resolve()) if len(sys.argv)>1 else "docs/SMPD_ALPR_Findings.md"
    out_path=str(Path(sys.argv[2]).resolve()) if len(sys.argv)>2 else str(Path(tempfile.gettempdir())/"SMPD_ALPR_Findings.pdf")
    if not md_path.endswith('.md') or not out_path.endswith('.pdf'):
        raise SystemExit("Error: expected .md input and .pdf output paths")
    with open(md_path,'rb') as f: _MD5_HASH=hashlib.md5(f.read()).hexdigest()
    dd=parse_md(md_path); S=mkstyles()
    frame=Frame(ML,MB,PAGE_W-ML-MR,PAGE_H-MT-MB,id='main')
    pdf=BaseDocTemplate(out_path,pagesize=letter,title="SMPD ALPR Investigation — Findings",author="",subject="ALPR")
    pdf.addPageTemplates([PageTemplate(id='cover',frames=[frame],onPage=on_cover),
                          PageTemplate(id='content',frames=[frame],onPage=on_content)])
    story=[]
    story.extend(build_cover(S,dd)); story.append(NextPageTemplate('content'))
    story.extend(build_toc(S,dd)); story.extend(build_exec(S,dd))
    for i,sec in enumerate(dd['sections']):
        story.append(PageBreak())
        story.extend(build_section(S,sec))
    story.append(PageBreak()); story.extend(build_source_table(S,dd))
    story.append(PageBreak()); story.extend(build_contacts(S,dd))
    story.append(PageBreak()); story.extend(build_appendix_a(S,dd))
    if dd.get('appendix_b_lines'):
        story.append(PageBreak()); story.extend(build_appendix_b(S,dd))
    if dd.get('verify_items'):
        story.append(Spacer(1,16)); story.extend(build_verify(S,dd))
    pdf.build(story)
    nb=sum(len(s['bullets']) for s in dd['sections'])
    nt=sum(1 for s in dd['sections'] if s.get('table_rows'))
    print(f"PDF: {out_path}")
    print(f"  Source MD5: {_MD5_HASH}")
    print(f"  {len(dd['sections'])} sections ({nt} with tables), {len(dd['source_rows'])} sources, {nb} bullets")
    print(f"  Appendix A: {len(dd['appendix_a_lines'])} lines, Appendix B: {len(dd['appendix_b_lines'])} lines")
    if dd['verify_items']: print(f"  Verify: {len(dd['verify_items'])} items")

if __name__=="__main__": main()

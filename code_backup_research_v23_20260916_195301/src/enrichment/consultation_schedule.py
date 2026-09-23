"""通常の午前・午後診療の間に明記された検査・手術枠を、診療時間表だけから確認。"""
from dataclasses import dataclass
import re
import unicodedata
from bs4 import BeautifulSoup

SIGNAL_NAME = "昼の検査・手術専用枠"
WEEKDAYS = "月火水木金土日"
PROCEDURE = re.compile(r"検査|手術|内視鏡|胃カメラ|大腸カメラ|処置|レーザー|特殊検査")
ORDINARY = re.compile(r"午前|午後|外来|診察|診療|受付時間")
NEGATIVE = re.compile(r"行っていません|行っておりません|行いません|行わない|実施していません|実施しておりません|実施しません|対応していません|実施なし|休止|中止|検討中|予定|予約受付|予約の受付|相談|説明|(?:検査|手術|内視鏡|処置)(?:は)?(?:なし|無し|休み|休診)")
RANGE = re.compile(
    r"(?P<p1>午前|午後|AM|PM)?\s*(?P<h1>\d{1,2})\s*(?::\s*(?P<m1>\d{2})|時(?:(?P<j1>\d{1,2})分)?)"
    r"\s*(?:[~〜～\-−‐‑–—―]|から)\s*"
    r"(?P<p2>午前|午後|AM|PM)?\s*(?P<h2>\d{1,2})\s*(?::\s*(?P<m2>\d{2})|時(?:(?P<j2>\d{1,2})分)?)",
    re.I,
)
SYMBOLS = "★☆●○◯〇◎△▲◇◆■□※＊*"


def clean(text):
    return unicodedata.normalize("NFKC",str(text or "")).strip()


def days(text):
    text=clean(text)
    result=set()
    if "平日" in text:
        result.update(range(5))
    for match in re.finditer(r"([月火水木金土日])(?:曜日?)?\s*[~〜～-]\s*([月火水木金土日])(?:曜日?)?",text):
        start,end=(WEEKDAYS.index(x) for x in match.groups())
        if start<=end:
            result.update(range(start,end+1))
    for match in re.finditer(r"([月火水木金土日])曜日?",text):
        result.add(WEEKDAYS.index(match[1]))
    if re.fullmatch(r"[月火水木金土日\s・、,/／]+",text):
        result.update(WEEKDAYS.index(x) for x in text if x in WEEKDAYS)
    return frozenset(result) if result else None


def kind(label,default=""):
    label=clean(label)
    if NEGATIVE.search(label):
        return ""
    if PROCEDURE.search(label):
        # 一般診療と並行する検査は、独立した専用枠としない。
        without_closure=re.sub(r"(?:一般)?(?:外来|診療|診察)(?:は)?(?:休診|なし|を休止|を行わない)","",label)
        if re.search(r"外来|診療|診察",without_closure):
            return ""
        return "procedure"
    if re.search(r"休診|休憩|昼休|受付のみ|予約のみ",label):
        return ""
    return "ordinary" if ORDINARY.search(label) else default


@dataclass(frozen=True)
class Slot:
    start: int
    end: int
    kind: str
    days: frozenset | None
    text: str


def slots(text,default="",day_set=None):
    text=clean(text)
    matches=list(RANGE.finditer(text))
    previous=0
    inherited=default
    found=[]
    for match in matches:
        prefix=text[previous:match.start()]
        label=text if len(matches)==1 else prefix
        role=kind(label,inherited)
        period1=(match['p1'] or ("午後" if "午後" in prefix else "午前" if "午前" in prefix else "")).upper()
        period2=(match['p2'] or period1).upper()
        values=[]
        for number,period in [(1,period1),(2,period2)]:
            hour=int(match[f'h{number}'])
            minute=int(match[f'm{number}'] or match[f'j{number}'] or 0)
            if period in {"午後","PM"} and hour<12:
                hour+=12
            elif (period=="AM" or (period=="午前" and number==1)) and hour==12:
                hour=0
            values.append(hour*60+minute if hour<=24 and minute<60 and (hour<24 or minute==0) else -1)
        start,end=values
        if role and 0<=start<end<=24*60:
            found.append(Slot(start,end,role,day_set if day_set is not None else days(label),label[:300]))
        previous=match.end()
        inherited=role
    return found


def grid(table):
    rows=[r for r in table.find_all('tr') if r.find_parent('table') is table]
    if len(rows)>80:
        return []
    cells={}
    width=0
    for y,row in enumerate(rows):
        x=0
        for cell in row.find_all(['th','td'],recursive=False):
            while (y,x) in cells:
                x+=1
            try:
                colspan=max(1,int(cell.get('colspan',1)))
                rowspan=max(1,int(cell.get('rowspan',1)))
            except (TypeError,ValueError):
                return []
            if x+colspan>24 or rowspan>80:
                return []
            for yy in range(y,min(len(rows),y+rowspan)):
                for xx in range(x,x+colspan):
                    cells[yy,xx]=clean(cell.get_text(' ',strip=True))
            x+=colspan
            width=max(width,x)
    return [[cells.get((y,x),'') for x in range(width)] for y in range(len(rows))]


def symbol_legend(soup):
    """★=手術、△：検査などの凡例を拾い、診療時間表の記号を意味に戻す。"""
    result={}
    # ページ全体を連結すると表の時刻まで凡例に混ざるため、短い独立ブロックだけを見る。
    for node in soup.find_all(['p','div','li','span','small','caption','td','th']):
        if node.find('table') is not None:
            continue
        text=clean(node.get_text(" ",strip=True))
        if not text or len(text)>120:
            continue
        for symbol in SYMBOLS:
            if symbol not in text:
                continue
            m=re.search(re.escape(symbol)+r"\s*(?:[:：=＝]|は)?\s*([^。;；]{0,60})",text)
            if not m or NEGATIVE.search(m.group(1)):
                continue
            proc=PROCEDURE.search(m.group(1))
            if proc:
                result[symbol]=proc.group(0)
    return result


def _expand_symbol(cell,legend):
    stripped=clean(cell)
    if stripped in legend:
        return stripped+" "+legend[stripped]
    # 「★（火曜）」等でも凡例がある場合だけ意味を付加。
    for symbol,meaning in legend.items():
        if symbol in stripped and not PROCEDURE.search(stripped):
            return stripped+" "+meaning
    return stripped


def table_slots(table,legend=None):
    legend=legend or {}
    matrix=grid(table)
    if not matrix or not any(matrix):
        return []
    matrix=[[_expand_symbol(cell,legend) for cell in row] for row in matrix]
    header=next(((i,{j:days(cell) for j,cell in enumerate(row) if days(cell)})
                 for i,row in enumerate(matrix) if sum(days(cell) is not None for cell in row)>=2),None)
    found=[]
    if header:
        row_index,columns=header
        first_day=min(columns)
        for row in matrix[row_index+1:]:
            if not row:
                continue
            prefix=' '.join(row[:first_day])
            for column,day_set in columns.items():
                if column>=len(row):
                    continue
                cell=row[column]
                if not RANGE.search(cell) and not re.fullmatch(r"[○◯〇●◎]+",cell) and not ORDINARY.search(cell) and not PROCEDURE.search(cell):
                    continue
                if not PROCEDURE.search(cell) and re.search(r"休診|休み|なし",cell):
                    continue
                selected_days=day_set & days(prefix) if days(prefix) else day_set
                if not selected_days:
                    continue
                text=prefix+' '+cell
                found.extend(slots(text,default='ordinary',day_set=selected_days))
        return found
    schedule=bool(re.search(r"診療|外来|午前|午後",table.get_text(' ',strip=True)))
    headings=matrix[0]
    for row in matrix:
        if not row:
            continue
        row_days=days(row[0])
        if row_days:
            for column,cell in enumerate(row[1:],1):
                heading=headings[column] if column<len(headings) else ''
                found.extend(slots((heading+' '+cell).strip(),default='ordinary' if schedule else '',day_set=row_days))
        else:
            found.extend(slots(' '.join(row),default='ordinary' if schedule else ''))
    return found


def proof(found):
    for day in range(7):
        same_day=[s for s in found if s.days is None or day in s.days]
        for procedure in same_day:
            if procedure.kind!='procedure':
                continue
            morning=[s for s in same_day if s.kind=='ordinary' and s.start<12*60 and s.end<=procedure.start]
            afternoon=[s for s in same_day if s.kind=='ordinary' and s.start>=12*60 and procedure.end<=s.start]
            if not morning or not afternoon:
                continue
            before=max(morning,key=lambda s:s.end)
            after=min(afternoon,key=lambda s:s.start)
            def fmt(slot):
                return f'{slot.start//60:02}:{slot.start%60:02}～{slot.end//60:02}:{slot.end%60:02}'
            label=WEEKDAYS[day]+'曜' if any(s.days is not None for s in [before,procedure,after]) else '共通時間表'
            return {'schedule_day':label,'morning_consultation':fmt(before),'procedure_slot':fmt(procedure),
                    'afternoon_consultation':fmt(after),
                    'evidence':f'{label}：午前診療 {fmt(before)}／検査・手術 {fmt(procedure)}／午後診療 {fmt(after)}',
                    'source_excerpt':procedure.text}
    return None


def midday_procedure(html):
    """診療時間だけから、午前と午後の間に明示された検査・手術・処置専用枠を確認。"""
    soup=BeautifulSoup(html,'html.parser')
    for node in soup.select('script,style,noscript,template'):
        node.decompose()
    legend=symbol_legend(soup)
    for table in soup.find_all('table'):
        if table.find('table') is not None:
            continue
        result=proof(table_slots(table,legend))
        if result:
            return {**result,'evidence_type':'公式HPの診療時間表で前後の通常診療と専用枠を確認'}
    for table in soup.find_all('table'):
        table.decompose()
    for br in soup.find_all('br'):
        br.replace_with('\n')
    groups={}
    for node in soup.find_all(['p','li','dd','div']):
        if node.find(['p','li','dd','div','table']) is not None:
            continue
        if node.find_parent(['p','li','dd']) is not None:
            continue
        heading=node.find_previous(['h1','h2','h3','h4','h5','h6'])
        container=node.find_parent(['section','article','div','main','body']) or soup
        key=(id(container),id(heading))
        default='ordinary' if heading and re.search(r"診療時間|外来時間|受付時間",heading.get_text()) else ''
        label=''
        if node.name=='dd':
            term=node.find_previous_sibling('dt')
            label=term.get_text(' ',strip=True) if term else ''
        for line in node.get_text(' ').splitlines():
            if len(line)<=1200:
                expanded=_expand_symbol(line,legend)
                groups.setdefault(key,[]).extend(slots(label+' '+expanded,default=default))
    for found in groups.values():
        result=proof(found)
        if result:
            return {**result,'evidence_type':'公式HPの診療時間案内で前後の通常診療と専用枠を確認'}
    return None

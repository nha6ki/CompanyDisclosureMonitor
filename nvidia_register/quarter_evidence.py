"""Resolve the release's period and read directly reported, consolidated quarterly values."""
import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import partial
import re
from xml.etree import ElementTree as ET

import alphabet
from companies import ISSUERS
import earnings
import filing_pair
import issuer_financials as issuers
import microsoft
import reconcile


FYE = {"AAPL": 9, "MSFT": 6, "NVDA": 1}
TAGS = [
    {"us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap:Revenues", "us-gaap:SalesRevenueNet"},
    {"us-gaap:EarningsPerShareDiluted", "us-gaap:EarningsPerShareBasicAndDiluted"},
]


def calendar_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def anchors(symbol, year):
    last = FYE.get(symbol, 12)
    for y in range(year-1, year+2):
        for q in range(1, 5):
            m = last - 3 * (4-q)
            yield calendar_end(y if m > 0 else y-1, (m-1)%12+1), y*4+q-1


def ordinal(symbol, end):
    anchor, key = min(anchors(symbol, end.year), key=lambda a: abs((a[0]-end).days))
    if abs((anchor-end).days)>8:
        raise ValueError("期末日が企業の四半期カレンダーと一致しません")
    return key


def label(key):
    return f"FY{key//4}Q{key%4+1}"


def period_candidates(symbol, day):
    for anchor, key in anchors(symbol, day.year):
        offsets = range(-8,9) if symbol in ("AAPL", "NVDA") else (0,)
        for delta in offsets:
            end = anchor + timedelta(days=delta)
            if not 0 <= (day-end).days <= 100:
                continue
            if symbol == "AAPL" and end.weekday()!=5 or symbol == "NVDA" and end.weekday()!=6:
                continue
            yield end, key


def numeric_rows(rows, column, getter):
    values, reasons = [], []
    for metric in filing_pair.METRICS:
        try:
            values.append(getter(rows,column,metric))
            reasons.append("")
        except ValueError as exc:
            values.append(None)
            reasons.append(str(exc))
    return values, reasons


def release(raw, symbol, filing_day):
    day = date.fromisoformat(filing_day)
    if symbol == "NVDA":
        facts = earnings.extract(raw)
        _, _, _, actual = reconcile.detail_table(raw, facts[0]["period"])
        end = datetime.strptime(actual, "%B %d, %Y").date()
        if not 0 <= (day-end).days <= 100:
            raise ValueError("提出日と決算期末が整合しません")
        checks = reconcile.compare(raw, facts)
        return end, [v['detail'].replace(',', '') or None for v in checks], [
            '' if v['detail'] else v['reason'] for v in checks]
    candidates = []
    if symbol == "TSLA":
        deck = issuers.SlideText()
        deck.feed(raw.decode("utf-8-sig"))
        if deck.statements:
            quarters = re.findall(r'Q([1-4])-(20\d\d)', deck.statements[0].partition(" REVENUES ")[0])
            if quarters:
                year, quarter = max((int(y),int(q)) for q,y in quarters)
                end = calendar_end(year,quarter*3)
                if 0 <= (day-end).days <= 100:
                    rows, col = issuers.tesla_slide(raw, f"Q{quarter}-{year}")
                    return (end, *numeric_rows(rows,col,issuers.value_from_slide))
        raise ValueError("Teslaの四半期損益計算書を特定できません")
    for end, _ in period_candidates(symbol,day):
        text = end.strftime("%B ") + str(end.day)
        try:
            if symbol=="GOOGL":
                rows, col = alphabet.statement(raw,text,str(end.year))
                getter = alphabet.metric_value
            elif symbol=="MSFT":
                rows, col = microsoft.statement(raw,text,str(end.year))
                getter = microsoft.metric_value
            else:
                rows, col = issuers.statement_table(raw,symbol,text+', '+str(end.year))
                getter = partial(issuers.value_from_grid,symbol=symbol)
        except ValueError:
            continue
        candidates.append((end,*numeric_rows(rows,col,getter)))
    if len(candidates)!=1:
        raise ValueError("決算添付から対象四半期を一意に特定できません")
    return candidates[0]


def periodic(raw, symbol, end):
    """Select non-segmented, 3-month XBRL facts for the issuer, end date and currency."""
    root = ET.fromstring(raw)
    contexts = {}
    for ctx in root.iterfind(".//{*}context"):
        ident = ctx.findtext(".//{*}identifier", "")
        if not ident.isdigit() or int(ident)!=ISSUERS[symbol][1]:
            continue
        if ctx.find(".//{*}segment") is not None or ctx.find(".//{*}scenario") is not None:
            continue
        start = ctx.findtext(".//{*}startDate")
        if start and ctx.findtext(".//{*}endDate")==end.isoformat() and 77 <= (end-date.fromisoformat(start)).days+1 <= 105:
            contexts[ctx.get("id")] = start
    units = {}
    for u in root.iterfind(".//{*}unit"):
        measures = [m.text for m in u.iterfind(".//{*}measure")]
        if measures == ["iso4217:USD"] and u.find("{*}divide") is None:
            units[u.get("id")] = 0
        elif (u.findtext(".//{*}unitNumerator/{*}measure")=="iso4217:USD" and
              u.findtext(".//{*}unitDenominator/{*}measure")=="xbrli:shares" and len(measures)==2):
            units[u.get("id")] = 1
    found = [set(),set()]
    evidence = [[],[]]
    for fact in root.iterfind(".//{*}nonFraction"):
        ctx = fact.get("contextRef")
        i = units.get(fact.get("unitRef"))
        if i is None or ctx not in contexts or fact.get("name") not in TAGS[i]:
            continue
        s = "".join(fact.itertext()).strip().replace(',','')
        if not re.fullmatch(r"\d+(?:\.\d+)?",s):
            continue
        value = Decimal(s).scaleb(int(fact.get("scale","0")) - (0 if i else 6))
        if fact.get('sign')=='-':
            value = -value
        found[i].add((contexts[ctx],value))
        evidence[i].append((fact.get('name'),ctx))
    values, reasons, locators = [], [], []
    for i, choices in enumerate(found):
        if len(choices)==1:
            start,value=choices.pop()
            values.append(format(value,'f'))
            tag,ctx=sorted(evidence[i])[0]
            locators.append(f"{tag} · {ctx} · {start} – {end}")
            reasons.append("")
        else:
            values.append(None)
            locators.append("")
            reasons.append("対象3か月の連結XBRL値なし" if not choices else "対象期間または金額に複数候補")
    return values,reasons,locators

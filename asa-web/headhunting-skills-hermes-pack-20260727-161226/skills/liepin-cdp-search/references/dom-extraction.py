"""
Liepin CDP card extraction — DOM TextNode approach.
DO NOT use regex on c.textContent — it mashes all fields together.
"""
import json

def extract_cards_via_cdp(cdp):
    """Extract structured candidate data from current Liepin search page via CDP.
    Returns list of {name, company, position, city, education}.
    """
    r = cdp.send("Runtime.evaluate", {
        "expression": """
        (() => {
            var cards = document.querySelectorAll('.tlog-common-resume-card');
            var data = [];
            cards.forEach(function(c) {
                // Walk text nodes individually — each field is a separate node
                var walker = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
                var nodes = [];
                while(walker.nextNode()) {
                    var t = walker.currentNode.textContent.trim();
                    if (t) nodes.push(t);
                }
                // nodes[0]: "今天活跃" / "隐藏活跃状态" / "7天内活跃"
                // nodes[1]: "刘**" (name+stars)
                // nodes[2]: "25岁"
                // nodes[3]: "工作3年"
                // nodes[4]: "本科" (education)
                // nodes[5]: "北京" (current city)
                // nodes[6]: "求职期望："
                // nodes[7]: "上海" (desired city)
                // ...later nodes: position keywords, company name

                var name = '', city = '', edu = '';
                for (var i = 0; i < nodes.length; i++) {
                    var n = nodes[i];
                    if (!name && /^[\\u4e00-\\u9fa5A-Za-z]{1,4}\\*{2}$/.test(n))
                        name = n.replace('**', '');
                    if (n === '求职期望：' && i+1 < nodes.length)
                        city = nodes[i+1];
                    if (!edu && /^(博士|硕士|本科|大专|MBA|Bachelor|Master)$/.test(n))
                        edu = n;
                }

                // Company: find known suffix pattern before "·" in full text
                var full = c.textContent.trim().replace(/\\s+/g, ' ');
                var cm = full.match(/([\\u4e00-\\u9fa5A-Za-z（）()·]+(?:有限公司|有限责任公司|科技股份有限公司|半导体有限公司|微电子|光电|技术有限公司|集团公司|集成电路|股份有限公司|半导体科技|半导体技术|半导体设备|科技集团))\\s*·/);
                var company = cm ? cm[1].trim().replace(/^.*[)）]\\s*/, '') : '';

                // Position: keyword after first "·"
                var dot = full.indexOf('·');
                var position = '';
                if (dot > 0) {
                    var after = full.substring(dot+1).trim();
                    var pm = after.match(/^([\\u4e00-\\u9fa5A-Za-z/]+(?:工程师|经理|总监|专家|主管|主任|顾问))/);
                    if (pm) position = pm[1];
                }

                // Clean company name: strip time-range prefixes like "年)", "个月)"
                company = company.replace(/^[^(]*[)）]/, '').trim();

                data.push({name: name, company: company, position: position, city: city, edu: edu});
            });
            return JSON.stringify({
                cnt: cards.length,
                total: document.querySelector('[data-nick=totalcnt]')?.textContent?.trim() || '?',
                data: data
            });
        })()
        """,
        "returnByValue": True
    })
    return json.loads(r['result']['result']['value'])


def fill_and_search(cdp, keyword):
    """Fill search box and click search button on h.liepin.com."""
    # Navigate to search page
    cdp.send("Page.navigate", {"url": "https://h.liepin.com/search/getConditionItem"})
    import time; time.sleep(4)

    # Fill keyword via React native setter
    cdp.send("Runtime.evaluate", {
        "expression": f"""(()=>{{
            var t=document.querySelector('#rc_select_1');
            var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
            s.call(t,'{keyword}');
            t.dispatchEvent(new Event('input',{{bubbles:true}}));
            t.dispatchEvent(new Event('change',{{bubbles:true}}));
        }})()""",
        "returnByValue": True
    })
    time.sleep(1.5)

    # Click search
    cdp.send("Runtime.evaluate", {
        "expression": "document.querySelector('button.search-btn').click()",
        "returnByValue": True
    })
    time.sleep(5)


def batch_search(cdp, keywords, db_path, client_name):
    """Run multiple Liepin searches and save to talent_pool.db.
    keywords: list of (keyword_string, label)
    """
    import sqlite3
    conn = sqlite3.connect(db_path)
    total_new = 0

    for kw, label in keywords:
        fill_and_search(cdp, kw)
        result = extract_cards_via_cdp(cdp)
        
        new = 0
        for c in result['data']:
            if not c['name'] or not c['company']:
                continue
            cur = conn.cursor()
            cur.execute(
                "INSERT OR IGNORE INTO candidates (name,company,position,education,city,source,client) VALUES (?,?,?,?,?,?,'liepin',?)",
                (c['name']+'**', c['company'][:60], c['position'][:30], c['edu'], c['city'][:20], client_name)
            )
            if cur.rowcount > 0:
                cur.execute(
                    "INSERT OR IGNORE INTO candidate_clients (candidate_name,candidate_company,client,source,position_tag) VALUES (?,?,?,?,?)",
                    (c['name']+'**', c['company'][:60], client_name, 'liepin', c['position'][:30])
                )
                new += 1
        
        conn.commit()
        total_new += new
    
    conn.close()
    return total_new

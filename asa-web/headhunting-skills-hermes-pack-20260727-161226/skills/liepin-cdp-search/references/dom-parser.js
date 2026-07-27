// Liepin Resume Card DOM Parser — use via CDP Runtime.evaluate
// Extracts name, company, position, city, education from .tlog-common-resume-card

(() => {
    var cards = document.querySelectorAll('.tlog-common-resume-card');
    return cards.map(function(c) {
        var walker = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
        var nodes = [];
        while(walker.nextNode()) {
            var t = walker.currentNode.textContent.trim();
            if (t) nodes.push(t);
        }
        
        var name = '', city = '', edu = '',
            full = c.textContent.trim().replace(/\s+/g, ' ');
        
        for (var i = 0; i < nodes.length; i++) {
            var n = nodes[i];
            if (!name && /^[\u4e00-\u9fa5A-Za-z]{1,4}\*{2}$/.test(n))
                name = n.replace('**', '');
            if (n === '求职期望：' && i + 1 < nodes.length)
                city = nodes[i + 1];
            if (!edu && /^(博士|硕士|本科|大专|MBA|Bachelor|Master)$/.test(n))
                edu = n;
        }
        
        // Company: text before "·" containing 有限公司/科技/集团/etc
        var cm = full.match(/([\u4e00-\u9fa5A-Za-z（）()·]+(?:有限公司|有限责任公司|科技股份有限公司|半导体有限公司|微电子|光电|技术有限公司|集团公司|集成电路|股份有限公司|半导体科技|半导体技术|半导体设备|科技集团))\s*·/);
        var company = cm ? cm[1].trim().replace(/^.*[)）]\s*/, '') : '';
        
        // Position: after first "·"
        var dot = full.indexOf('·');
        var position = '';
        if (dot > 0) {
            var pm = full.substring(dot + 1).trim().match(/^([\u4e00-\u9fa5A-Za-z/]+(?:工程师|经理|总监|专家|主管|主任|顾问))/);
            if (pm) position = pm[1];
        }
        
        return {name: name + '**', company: company, position: position, city: city, edu: edu};
    });
})()

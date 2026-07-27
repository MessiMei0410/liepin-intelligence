(function(){
if(document.getElementById("__he"))return;
var b=document.createElement("div");
b.id="__he";b.textContent="📄导出docx";
b.style.cssText="position:fixed;bottom:100px;right:30px;z-index:9998;background:linear-gradient(135deg,#1a478a,#2563eb);color:#fff;padding:10px 22px;border-radius:20px;cursor:pointer;font:bold 14px system-ui;box-shadow:0 3px 15px rgba(26,71,138,.35);transition:all .2s";
b.onmouseenter=function(){b.style.transform="scale(1.05)";b.style.boxShadow="0 5px 20px rgba(26,71,138,.5)"};
b.onmouseleave=function(){b.style.transform="scale(1)";b.style.boxShadow="0 3px 15px rgba(26,71,138,.35)"};
b.onclick=function(){
  b.textContent="⏳";
  window.__he=JSON.stringify({t:(document.body.innerText||"").substring(0,20000),u:window.location.href});
  setTimeout(function(){b.textContent="📤已采集";b.style.background="linear-gradient(135deg,#22c55e,#16a34a)"},300);
  setTimeout(function(){b.textContent="📄导出docx";b.style.background="linear-gradient(135deg,#1a478a,#2563eb)"},2500);
};
document.body.appendChild(b);
})()
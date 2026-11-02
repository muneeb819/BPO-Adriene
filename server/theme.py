"""The console's design system: one stylesheet, one shell, three pages.

Before this, each page carried its own <style> block with flat slate colours and
a row of grey nav links — the dashboard, the debug console and the campaign
manager looked like three different applications, because they were.

Everything here is inlined into the page rather than served from a /static
route. That is deliberate: the console has to render from a single HTML
response with no second request, so it works behind the basic-auth middleware,
inside the packaged build, and in a preview iframe with no network.

Contents
    THEME_CSS   the whole design system — tokens, shell, cards, tables,
                buttons, forms, transcript, waveforms, animations
    shell()     <head> + animated backdrop + top navigation, shared by all pages
    page()      shell() wrapped around a page body
"""
from __future__ import annotations

# --------------------------------------------------------------------------- #
# Design tokens + stylesheet
# --------------------------------------------------------------------------- #

THEME_CSS = """
/* ------------------------------------------------------------------ *
 *  Tokens
 * ------------------------------------------------------------------ */
:root{
  --bg:#060911; --bg-2:#0a101d;
  --panel:rgba(255,255,255,.045); --panel-2:rgba(255,255,255,.075);
  --stroke:rgba(255,255,255,.09); --stroke-2:rgba(255,255,255,.16);
  --ink:#e8eefb; --ink-2:#a9b7d0; --ink-3:#6d7e99;
  --cyan:#22d3ee; --indigo:#6366f1; --violet:#a855f7;
  --green:#34d399; --amber:#fbbf24; --red:#fb7185;
  --grad:linear-gradient(135deg,var(--cyan),var(--indigo) 55%,var(--violet));
  --r-sm:10px; --r:16px; --r-lg:22px; --r-pill:999px;
  --shadow:0 1px 1px rgba(0,0,0,.3),0 8px 24px -8px rgba(0,0,0,.55),0 24px 56px -24px rgba(0,0,0,.7);
  --glow:0 0 0 1px rgba(34,211,238,.35),0 0 28px -6px rgba(34,211,238,.45);
  --ease:cubic-bezier(.2,.8,.25,1);
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}

*,*::before,*::after{box-sizing:border-box}

html{-webkit-text-size-adjust:100%}

body{
  margin:0; min-height:100vh; color:var(--ink);
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Inter,Roboto,sans-serif;
  font-size:15px; line-height:1.6; letter-spacing:-.011em;
  background:var(--bg); overflow-x:hidden;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
}

/* ------------------------------------------------------------------ *
 *  Backdrop — aurora blobs + grid, fixed behind everything
 * ------------------------------------------------------------------ */
.bd{position:fixed;inset:0;z-index:-1;overflow:hidden;background:
  radial-gradient(1200px 600px at 12% -8%, rgba(34,211,238,.16), transparent 60%),
  radial-gradient(1000px 520px at 88% 0%, rgba(168,85,247,.15), transparent 62%),
  radial-gradient(900px 700px at 50% 110%, rgba(99,102,241,.14), transparent 65%),
  linear-gradient(180deg,var(--bg),var(--bg-2))}
.bd::before{
  content:""; position:absolute; inset:-30%;
  background-image:linear-gradient(rgba(255,255,255,.045) 1px,transparent 1px),
                   linear-gradient(90deg,rgba(255,255,255,.045) 1px,transparent 1px);
  background-size:56px 56px;
  mask-image:radial-gradient(circle at 50% 0%,#000 0%,transparent 72%);
  -webkit-mask-image:radial-gradient(circle at 50% 0%,#000 0%,transparent 72%);
  animation:drift 60s linear infinite;
}
.bd::after{
  content:""; position:absolute; width:56vw; height:56vw; left:-14vw; top:-18vw;
  background:conic-gradient(from 0deg,rgba(34,211,238,.22),rgba(99,102,241,.18),rgba(168,85,247,.22),rgba(34,211,238,.22));
  filter:blur(90px); border-radius:50%; opacity:.5; animation:orbit 44s linear infinite;
}
@keyframes drift{to{transform:translate3d(-56px,-56px,0)}}
@keyframes orbit{to{transform:rotate(360deg)}}

/* ------------------------------------------------------------------ *
 *  Shell — top bar
 * ------------------------------------------------------------------ */
.top{
  position:sticky;top:0;z-index:50;
  display:flex;align-items:center;gap:1.25rem;flex-wrap:wrap;
  padding:.85rem clamp(1rem,3vw,2.25rem);
  background:linear-gradient(180deg,rgba(6,9,17,.86),rgba(6,9,17,.62));
  backdrop-filter:blur(20px) saturate(160%);
  -webkit-backdrop-filter:blur(20px) saturate(160%);
  border-bottom:1px solid var(--stroke);
}
.brand{display:flex;align-items:center;gap:.7rem;text-decoration:none;color:inherit;
  transition:transform .35s var(--ease)}
.brand:hover{transform:translateY(-1px)}
.brand svg{filter:drop-shadow(0 0 14px rgba(34,211,238,.5))}
.brand b{font-size:1.02rem;font-weight:700;letter-spacing:-.02em;
  background:linear-gradient(120deg,#fff,#bfd4ff 60%,#9db4ff);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.brand small{display:block;font-size:.68rem;color:var(--ink-3);font-weight:500;
  letter-spacing:.09em;text-transform:uppercase;margin-top:-2px}

.nav{display:flex;gap:.3rem;flex-wrap:wrap;margin-left:auto;
  padding:.25rem;border-radius:var(--r-pill);background:rgba(255,255,255,.04);
  border:1px solid var(--stroke)}
.nav a{
  display:inline-flex;align-items:center;gap:.45rem;
  padding:.5rem .95rem;border-radius:var(--r-pill);
  font-size:.85rem;font-weight:600;color:var(--ink-2);text-decoration:none;
  transition:color .25s var(--ease),background .25s var(--ease),transform .25s var(--ease);
  white-space:nowrap;
}
.nav a:hover{color:#fff;background:rgba(255,255,255,.07);transform:translateY(-1px)}
.nav a[aria-current="page"]{
  color:#04121a;background:var(--grad);box-shadow:0 6px 22px -8px rgba(99,102,241,.9)}

.live{display:inline-flex;align-items:center;gap:.5rem;padding:.4rem .8rem;
  border-radius:var(--r-pill);background:rgba(52,211,153,.1);
  border:1px solid rgba(52,211,153,.32);color:#8ff3cd;
  font-size:.74rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase}
.live i{width:7px;height:7px;border-radius:50%;background:var(--green);
  box-shadow:0 0 0 0 rgba(52,211,153,.6);animation:ping 2s infinite}
@keyframes ping{70%{box-shadow:0 0 0 10px rgba(52,211,153,0)}100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}

/* ------------------------------------------------------------------ *
 *  Layout
 * ------------------------------------------------------------------ */
.wrap{max-width:1240px;margin:0 auto;padding:clamp(1.25rem,3vw,2.5rem) clamp(1rem,3vw,2.25rem) 4rem}

.hero{margin-bottom:1.75rem;animation:rise .7s var(--ease) both}
.hero h1{margin:0;font-size:clamp(1.7rem,3.4vw,2.5rem);line-height:1.12;
  letter-spacing:-.035em;font-weight:800;
  background:linear-gradient(110deg,#fff 20%,#a5f3fc 50%,#c4b5fd 80%);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{margin:.55rem 0 0;color:var(--ink-2);font-size:.97rem;max-width:62ch}

.section{margin-top:2.25rem;animation:rise .7s var(--ease) both}
.section-head{display:flex;align-items:center;gap:.75rem;margin-bottom:.9rem;flex-wrap:wrap}
.section-head h2{margin:0;font-size:1.05rem;font-weight:700;letter-spacing:-.02em;
  display:flex;align-items:center;gap:.55rem}
.section-head h2::before{content:"";width:4px;height:18px;border-radius:2px;
  background:var(--grad);box-shadow:0 0 14px rgba(99,102,241,.7)}
.section-head .spacer{margin-left:auto}

@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}

/* ------------------------------------------------------------------ *
 *  Cards / stats
 * ------------------------------------------------------------------ */
.stats{display:grid;gap:.9rem;grid-template-columns:repeat(auto-fit,minmax(158px,1fr))}
.stat{
  position:relative;overflow:hidden;
  background:var(--panel);border:1px solid var(--stroke);border-radius:var(--r);
  padding:1rem 1.1rem;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  box-shadow:var(--shadow);
  transition:transform .35s var(--ease),border-color .35s var(--ease),background .35s var(--ease);
}
.stat::after{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;
  background:radial-gradient(120% 90% at 100% 0%,rgba(99,102,241,.18),transparent 60%);opacity:0;
  transition:opacity .35s var(--ease)}
.stat:hover{transform:translateY(-3px);border-color:var(--stroke-2);background:var(--panel-2)}
.stat:hover::after{opacity:1}
.stat .label{font-size:.66rem;text-transform:uppercase;letter-spacing:.11em;
  color:var(--ink-3);font-weight:700}
.stat .val{margin-top:.3rem;font-size:1.28rem;font-weight:750;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums}
.stat .val.mono{font-family:var(--mono);font-size:1.05rem}
.stat .val.grad{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}

/* ------------------------------------------------------------------ *
 *  Panels
 * ------------------------------------------------------------------ */
.panel{
  background:var(--panel);border:1px solid var(--stroke);border-radius:var(--r-lg);
  padding:1.15rem;backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
  box-shadow:var(--shadow);
}

/* ------------------------------------------------------------------ *
 *  Tables
 * ------------------------------------------------------------------ */
.tbl-wrap{overflow:auto;border-radius:var(--r);border:1px solid var(--stroke);
  background:rgba(255,255,255,.025)}
table{border-collapse:separate;border-spacing:0;width:100%}
thead th{
  position:sticky;top:0;z-index:1;
  background:rgba(10,16,29,.92);backdrop-filter:blur(12px);
  padding:.7rem .9rem;text-align:left;font-size:.68rem;font-weight:700;
  letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3);
  border-bottom:1px solid var(--stroke);white-space:nowrap}
tbody td{padding:.72rem .9rem;font-size:.86rem;border-bottom:1px solid rgba(255,255,255,.05);
  color:var(--ink-2)}
tbody tr{transition:background .2s var(--ease)}
tbody tr:hover td{background:rgba(255,255,255,.04);color:var(--ink)}
tbody tr:last-child td{border-bottom:0}
td.mono,.mono{font-family:var(--mono);font-size:.82rem;font-variant-numeric:tabular-nums}

/* ------------------------------------------------------------------ *
 *  Pills / tags / badges
 * ------------------------------------------------------------------ */
.pill,.tag,.badge{display:inline-flex;align-items:center;gap:.35rem;
  padding:.2rem .6rem;border-radius:var(--r-pill);
  font-size:.72rem;font-weight:700;letter-spacing:.02em;
  background:rgba(255,255,255,.07);border:1px solid var(--stroke);color:var(--ink-2)}
.tag.ok,.pill.ok,.badge.ok{background:rgba(52,211,153,.12);border-color:rgba(52,211,153,.34);color:#8ff3cd}
.tag.hangup,.badge.hangup{background:rgba(251,113,133,.12);border-color:rgba(251,113,133,.34);color:#ffb4c1}
.badge.live{background:rgba(34,211,238,.14);border-color:rgba(34,211,238,.38);color:#a5f3fc}

/* ------------------------------------------------------------------ *
 *  Buttons
 * ------------------------------------------------------------------ */
button,.btn{
  font:inherit;font-weight:650;font-size:.85rem;
  display:inline-flex;align-items:center;justify-content:center;gap:.5rem;
  padding:.6rem 1.1rem;border-radius:var(--r-pill);cursor:pointer;
  color:var(--ink);background:rgba(255,255,255,.07);
  border:1px solid var(--stroke-2);
  transition:transform .2s var(--ease),background .2s var(--ease),
             border-color .2s var(--ease),box-shadow .25s var(--ease);
}
button:hover,.btn:hover{background:rgba(255,255,255,.12);transform:translateY(-1px)}
button:active,.btn:active{transform:translateY(0) scale(.985)}
button:disabled{opacity:.45;cursor:not-allowed;transform:none}
button.primary,.btn.primary{background:var(--grad);border:0;color:#04121a;
  box-shadow:0 8px 26px -10px rgba(99,102,241,.95)}
button.primary:hover{box-shadow:0 12px 34px -10px rgba(99,102,241,1);filter:brightness(1.06)}
button.danger{background:rgba(251,113,133,.14);border-color:rgba(251,113,133,.4);color:#ffb4c1}
button.danger:hover{background:rgba(251,113,133,.22)}
button.ghost{background:transparent}
button.ghost:hover{background:rgba(255,255,255,.07)}

/* big call controls */
.big{padding:.95rem 1.7rem;font-size:1rem;border-radius:var(--r-pill);border:0;font-weight:750}
.big.start{background:linear-gradient(135deg,#34d399,#10b981);color:#04121a;
  box-shadow:0 10px 30px -12px rgba(16,185,129,.95)}
.big.stop{background:linear-gradient(135deg,#fb7185,#e11d48);color:#fff;
  box-shadow:0 10px 30px -12px rgba(225,29,72,.9)}
.big:disabled{background:rgba(255,255,255,.08);box-shadow:none;color:var(--ink-3)}

/* ------------------------------------------------------------------ *
 *  Forms
 * ------------------------------------------------------------------ */
input[type=text],input[type=number],input[type=password],input[type=email],select,textarea{
  font:inherit;font-size:.88rem;color:var(--ink);
  background:rgba(255,255,255,.05);border:1px solid var(--stroke-2);
  border-radius:var(--r-sm);padding:.62rem .8rem;width:100%;
  transition:border-color .2s var(--ease),box-shadow .2s var(--ease),background .2s var(--ease);
}
input::placeholder,textarea::placeholder{color:var(--ink-3)}
input:focus,select:focus,textarea:focus{
  outline:none;border-color:rgba(34,211,238,.6);background:rgba(255,255,255,.08);
  box-shadow:0 0 0 4px rgba(34,211,238,.14)}
select{appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%236d7e99' stroke-width='1.8' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right .75rem center;padding-right:2rem}
label{font-size:.8rem;font-weight:600;color:var(--ink-2)}
a{color:#7dd3fc;text-decoration-color:rgba(125,211,252,.35);text-underline-offset:2px}
a:hover{color:#bae6fd}

/* ------------------------------------------------------------------ *
 *  Transcript / chat
 * ------------------------------------------------------------------ */
#chat,.chat{
  display:flex;flex-direction:column;gap:.6rem;
  padding:1.1rem;min-height:320px;max-height:52vh;overflow-y:auto;
  background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.02));
  border:1px solid var(--stroke);border-radius:var(--r-lg);
  scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.22) transparent;
}
#chat::-webkit-scrollbar,.chat::-webkit-scrollbar{width:9px}
#chat::-webkit-scrollbar-thumb,.chat::-webkit-scrollbar-thumb{
  background:rgba(255,255,255,.18);border-radius:99px;border:2px solid transparent;
  background-clip:padding-box}
#chat::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.32);background-clip:padding-box}

.msg{
  max-width:80%;padding:.7rem 1rem;border-radius:18px;font-size:.9rem;line-height:1.55;
  animation:pop .38s var(--ease) both;position:relative;word-break:break-word;
}
.msg.agent{
  align-self:flex-start;color:#eafbff;
  background:linear-gradient(135deg,rgba(34,211,238,.22),rgba(99,102,241,.24));
  border:1px solid rgba(34,211,238,.26);border-bottom-left-radius:6px;
  box-shadow:0 10px 30px -18px rgba(34,211,238,.9)}
.msg.caller{
  align-self:flex-end;color:#fff;
  background:linear-gradient(135deg,rgba(255,255,255,.14),rgba(255,255,255,.07));
  border:1px solid var(--stroke-2);border-bottom-right-radius:6px}
.msg.sys{align-self:center;max-width:100%;font-size:.76rem;color:var(--ink-3);
  background:transparent;border:1px dashed rgba(255,255,255,.14);text-align:center}
.msg.partial{align-self:flex-end;opacity:.62;font-style:italic}
@keyframes pop{from{opacity:0;transform:translateY(8px) scale(.97)}to{opacity:1;transform:none}}

/* typing indicator */
.typing{display:inline-flex;gap:4px;padding:.2rem 0}
.typing i{width:6px;height:6px;border-radius:50%;background:var(--cyan);animation:bounce 1.2s infinite}
.typing i:nth-child(2){animation-delay:.15s}.typing i:nth-child(3){animation-delay:.3s}
@keyframes bounce{0%,60%,100%{transform:translateY(0);opacity:.45}30%{transform:translateY(-5px);opacity:1}}

/* ------------------------------------------------------------------ *
 *  Audio wave
 * ------------------------------------------------------------------ */
.wave{display:flex;align-items:center;gap:3px;height:26px}
.wave i{width:3px;border-radius:2px;background:var(--grad);
  animation:eq 1s ease-in-out infinite;opacity:.85}
.wave i:nth-child(1){height:30%;animation-delay:0s}
.wave i:nth-child(2){height:70%;animation-delay:.1s}
.wave i:nth-child(3){height:45%;animation-delay:.2s}
.wave i:nth-child(4){height:95%;animation-delay:.3s}
.wave i:nth-child(5){height:55%;animation-delay:.4s}
.wave i:nth-child(6){height:80%;animation-delay:.5s}
.wave i:nth-child(7){height:35%;animation-delay:.6s}
@keyframes eq{0%,100%{transform:scaleY(.42)}50%{transform:scaleY(1)}}

/* ------------------------------------------------------------------ *
 *  Skeleton shimmer
 * ------------------------------------------------------------------ */
.skel{height:14px;border-radius:8px;
  background:linear-gradient(90deg,rgba(255,255,255,.06) 25%,rgba(255,255,255,.14) 37%,rgba(255,255,255,.06) 63%);
  background-size:400% 100%;animation:shimmer 1.4s ease-in-out infinite}
@keyframes shimmer{0%{background-position:100% 0}100%{background-position:0 0}}


/* ------------------------------------------------------------------ *
 *  Campaign cards
 *
 *  .ccard is the class the campaign manager's renderCards() emits. It had
 *  its own flat styling before the theme existed, so it is styled by name
 *  here rather than by rewriting that page's JavaScript.
 * ------------------------------------------------------------------ */
#cards{display:grid;gap:.9rem;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));margin-top:.4rem}
.ccard{
  position:relative;display:flex;flex-direction:column;gap:.55rem;
  padding:1.1rem 1.15rem;border-radius:var(--r);
  background:var(--panel);border:1px solid var(--stroke);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  box-shadow:var(--shadow);
  transition:transform .35s var(--ease),border-color .35s var(--ease),background .35s var(--ease);
}
.ccard:hover{transform:translateY(-3px);border-color:var(--stroke-2);background:var(--panel-2)}
.ccard.active{border-color:rgba(52,211,153,.42);
  background:linear-gradient(160deg,rgba(52,211,153,.1),var(--panel) 55%)}
.ccard.active::before{content:"";position:absolute;left:0;top:14px;bottom:14px;width:3px;
  border-radius:0 3px 3px 0;background:linear-gradient(180deg,var(--green),#10b981)}
.ccard h3{margin:0;font-size:1.02rem;font-weight:700;letter-spacing:-.02em;color:#fff}
.ccard .muted{font-size:.75rem;text-transform:uppercase;letter-spacing:.09em;
  color:var(--ink-3);font-weight:700}
.ccard .desc{font-size:.87rem;color:var(--ink-2);line-height:1.55}
.ccard .crow{display:flex;gap:.45rem;flex-wrap:wrap;margin-top:.35rem}
.ccard .crow button{font-size:.78rem;padding:.45rem .85rem}
.ccard .b-deploy{background:var(--grad);border:0;color:#04121a;font-weight:750}
.ccard .b-danger{background:rgba(251,113,133,.14);border-color:rgba(251,113,133,.4);color:#ffb4c1}
.ccard .badge{background:rgba(52,211,153,.14);border-color:rgba(52,211,153,.4);color:#8ff3cd;
  align-self:flex-start}

/* ------------------------------------------------------------------ *
 *  Utilities
 * ------------------------------------------------------------------ */
.muted{color:var(--ink-3);font-size:.84rem}
.row{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
.grid-2{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
hr{border:0;border-top:1px solid var(--stroke);margin:1.5rem 0}

/* ------------------------------------------------------------------ *
 *  Accessibility — honour reduced motion, keep focus visible
 * ------------------------------------------------------------------ */
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;
    transition-duration:.001ms!important}
}
:focus-visible{outline:2px solid var(--cyan);outline-offset:2px;border-radius:6px}
::selection{background:rgba(99,102,241,.45);color:#fff}

/* ------------------------------------------------------------------ *
 *  Responsive
 * ------------------------------------------------------------------ */
@media (max-width:720px){
  .top{padding:.7rem 1rem}
  .nav{width:100%;margin-left:0;overflow-x:auto;flex-wrap:nowrap}
  .brand small{display:none}
  .msg{max-width:92%}
}
"""

# --------------------------------------------------------------------------- #
# Shell
# --------------------------------------------------------------------------- #

#: Inline SVG mark — no external asset, works in a sandboxed iframe.
_MARK = (
    '<svg width="30" height="30" viewBox="0 0 32 32" fill="none" aria-hidden="true">'
    '<defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#22d3ee"/><stop offset=".55" stop-color="#6366f1"/>'
    '<stop offset="1" stop-color="#a855f7"/></linearGradient></defs>'
    '<rect x="1.4" y="1.4" width="29.2" height="29.2" rx="9.2" stroke="url(#lg)" stroke-width="1.6"/>'
    '<path d="M10 21.5V12" stroke="url(#lg)" stroke-width="2.4" stroke-linecap="round"/>'
    '<path d="M16 24V9" stroke="url(#lg)" stroke-width="2.4" stroke-linecap="round"/>'
    '<path d="M22 19.5V13" stroke="url(#lg)" stroke-width="2.4" stroke-linecap="round"/>'
    '</svg>'
)


def shell(title: str, active: str = "", body: str = "") -> str:
    """Wrap a page body in the shared shell.

    `active` is one of dashboard / debug / campaigns / studio; it sets
    aria-current so the nav pill for the current page is highlighted.
    """
    items = [
        ("dashboard", "/", "Dashboard", "&#128202;"),
        ("debug", "/debug", "Live Voice", "&#127908;"),
        ("campaigns", "/campaigns", "Campaigns", "&#9881;&#65039;"),
        ("studio", "/studio", "Voice Studio", "&#127897;"),
    ]
    links = "".join(
        f'<a href="{href}"{" aria-current=page" if key == active else ""}>'
        f'<span aria-hidden="true">{icon}</span>{label}</a>'
        for key, href, label, icon in items
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#060911">
<title>{title}</title>
<style>{THEME_CSS}</style>
</head>
<body>
<div class="bd" aria-hidden="true"></div>
<header class="top">
  <a class="brand" href="/">
    {_MARK}
    <span><b>Adriene</b><small>Voice Agent Platform</small></span>
  </a>
  <nav class="nav" aria-label="Sections">{links}</nav>
</header>
<main class="wrap">
{body}
</main>
</body></html>"""

// 介绍页交互(PRD §3.1 / A19):
//   - 三场景切换:先 decode,再以请求序号防止快速点击乱序;active class 驱动 650ms 过渡;
//   - 首屏仅首次访问播放 WAAPI 入场;图库/示意图 IntersectionObserver 首次进入播放一次;
//   - 大图 dialog 200ms 进 / 150ms 退,Esc 与关闭共用退出流程,焦点返回触发卡片;
//   - 后台/离开(pagehide)取消动画与观察;减少动态偏好即时取消;
//   - 「查看示例画布」只创建明确标识的本机示例(幂等 key 固定),不调用模型。

/* ---------- 内联 SVG 作品(本地生成,无外部资源、无模型调用) ---------- */

const svgUrl = (svg) => `data:image/svg+xml;utf8,${encodeURIComponent(svg.replace(/\s+/g, ' ').trim())}`

/** @param {string} stops @param {string} shapes */
const art = (stops, shapes) =>
  svgUrl(`<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1600 1000'>
    <defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>${stops}</linearGradient></defs>
    <rect width='1600' height='1000' fill='url(#g)'/>${shapes}</svg>`)

const ocean = art(
  `<stop offset='0' stop-color='#0b3d5c'/><stop offset='.55' stop-color='#1f7a99'/><stop offset='1' stop-color='#e8d9b8'/>`,
  `<circle cx='1180' cy='260' r='86' fill='#f6e9c8' opacity='.92'/>
   <path d='M0 640 Q400 560 800 640 T1600 640 V1000 H0 Z' fill='#0d2f45' opacity='.85'/>
   <path d='M0 740 Q400 660 800 740 T1600 740 V1000 H0 Z' fill='#0a2436' opacity='.9'/>
   <path d='M0 560 Q400 500 800 560 T1600 560' stroke='#bfe3ea' stroke-width='6' fill='none' opacity='.5'/>`,
)

const city = art(
  `<stop offset='0' stop-color='#2b2f3a'/><stop offset='1' stop-color='#c98a4b'/>`,
  `<rect x='120' y='430' width='180' height='570' fill='#1d2027'/>
   <rect x='360' y='300' width='150' height='700' fill='#262a34'/>
   <rect x='560' y='520' width='220' height='480' fill='#181b22'/>
   <rect x='840' y='240' width='170' height='760' fill='#22262f'/>
   <rect x='1060' y='470' width='240' height='530' fill='#1a1d25'/>
   <rect x='1350' y='350' width='160' height='650' fill='#262a34'/>
   <g fill='#f3d9a4' opacity='.85'>
     <rect x='390' y='340' width='16' height='16'/><rect x='430' y='340' width='16' height='16'/>
     <rect x='880' y='290' width='16' height='16'/><rect x='920' y='330' width='16' height='16'/>
     <rect x='1100' y='520' width='16' height='16'/><rect x='1390' y='400' width='16' height='16'/>
   </g>`,
)

const flower = art(
  `<stop offset='0' stop-color='#5a2540'/><stop offset='.6' stop-color='#a34a63'/><stop offset='1' stop-color='#e8b88a'/>`,
  `<g transform='translate(800 520)'>
     <g fill='#f2d7dd'>
       <ellipse rx='70' ry='190' transform='rotate(0)'/><ellipse rx='70' ry='190' transform='rotate(45)'/>
       <ellipse rx='70' ry='190' transform='rotate(90)'/><ellipse rx='70' ry='190' transform='rotate(135)'/>
     </g>
     <circle r='86' fill='#e8b455'/>
     <circle r='46' fill='#a3552f'/>
   </g>
   <g fill='#7c2f47' opacity='.7'>
     <circle cx='300' cy='220' r='46'/><circle cx='1330' cy='820' r='60'/><circle cx='1240' cy='160' r='30'/>
   </g>`,
)

const dune = art(
  `<stop offset='0' stop-color='#7a4a2b'/><stop offset='1' stop-color='#e6c68f'/>`,
  `<path d='M0 700 Q500 520 1000 680 T1600 640 V1000 H0 Z' fill='#5d3a22'/>
   <path d='M0 820 Q600 680 1600 800 V1000 H0 Z' fill='#43291a'/>
   <circle cx='420' cy='240' r='70' fill='#f7e7c3' opacity='.9'/>`,
)

const tide = art(
  `<stop offset='0' stop-color='#123f4d'/><stop offset='1' stop-color='#9ec8c3'/>`,
  `<g stroke='#eef7f2' fill='none' opacity='.6'>
     <path d='M0 300 Q400 220 800 300 T1600 300' stroke-width='8'/>
     <path d='M0 520 Q400 440 800 520 T1600 520' stroke-width='6'/>
     <path d='M0 740 Q400 660 800 740 T1600 740' stroke-width='4'/>
   </g>`,
)

const bloom = art(
  `<stop offset='0' stop-color='#33445f'/><stop offset='1' stop-color='#d9a7c7'/>`,
  `<g fill='#f4e3ef' opacity='.9'>
     <circle cx='520' cy='420' r='150'/><circle cx='700' cy='560' r='110'/><circle cx='380' cy='600' r='90'/>
   </g>
   <circle cx='560' cy='500' r='60' fill='#d9856b'/>`,
)

const SCENES = [
  { name: '海面之上', caption: '海面之上 · 远景留白把环境交还给光', art: ocean },
  { name: '城市几何', caption: '城市几何 · 方向光切割体块的秩序', art: city },
  { name: '花与光', caption: '花与光 · 特写聚焦最具辨识度的细节', art: flower },
]

const WORKS = [
  { title: '沙丘 · 黄昏', caption: '展示图:暖调风景', art: dune },
  { title: '潮汐 · 线', caption: '展示图:抽象海浪', art: tide },
  { title: '绽放 · 粉', caption: '展示图:静物花', art: bloom },
  { title: '城市 · 光', caption: '展示图:建筑几何', art: city },
]

/** @param {string} sel @returns {HTMLElement} */
const $ = (sel) => /** @type {HTMLElement} */ (document.querySelector(sel))

const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)')

/* ---------- 场景装配 ---------- */

function mountArt(el, url) {
  // 先 decode 再上屏(A19:先 decode,防快速切换花屏)
  const img = new Image()
  img.src = url
  img.decoding = 'async'
  const apply = () => {
    el.style.backgroundImage = `url("${url}")`
  }
  if (img.decode) {
    img.decode().then(apply, apply)
  } else if (img.complete) {
    apply()
  } else {
    img.onload = apply
  }
}

let activeScene = 0
let sceneSeq = 0

/** @param {number} index */
async function showScene(index) {
  const seq = ++sceneSeq
  const scene = SCENES[index]
  const media = $('#scene-media')
  activeScene = index
  // 选中态 / 说明 / aria 同步
  for (const tab of document.querySelectorAll('.scene-tab')) {
    const selected = Number(tab.getAttribute('data-scene')) === index
    tab.classList.toggle('is-active', selected)
    tab.setAttribute('aria-selected', String(selected))
  }
  $('#scene-caption').textContent = scene.caption
  if (reduceMotion.matches || !media.animate) {
    media.classList.remove('is-swapping')
    mountArt(media, scene.art)
    return
  }
  media.classList.add('is-swapping')
  await new Promise((resolve) => setTimeout(resolve, 180))
  if (seq !== sceneSeq) {
    return // 已被更新的点击取代:丢弃这次切换
  }
  mountArt(media, scene.art)
  media.classList.remove('is-swapping')
}

for (const thumb of document.querySelectorAll('[data-thumb]')) {
  mountArt(/** @type {HTMLElement} */ (thumb), SCENES[Number(thumb.getAttribute('data-thumb'))].art)
}
mountArt($('#scene-media'), SCENES[0].art)
mountArt($('#hero-media'), SCENES[0].art)
$('#scene-caption').textContent = SCENES[0].caption
for (const tab of document.querySelectorAll('.scene-tab')) {
  tab.addEventListener('click', () => {
    const index = Number(tab.getAttribute('data-scene'))
    if (index !== activeScene) {
      void showScene(index)
    }
  })
}

/* ---------- 首屏入场:仅首次访问(WAAPI);减少动态直接跳过 ---------- */

const HERO_SEEN_KEY = 'landing.heroSeen'
let heroAnimations = []
try {
  if (localStorage.getItem(HERO_SEEN_KEY) || reduceMotion.matches || !document.animate) {
    document.body.classList.add('no-hero-anim')
  } else {
    localStorage.setItem(HERO_SEEN_KEY, '1')
    heroAnimations.push(
      $('#hero-media').animate(
        [{ transform: 'scale(1.10)' }, { transform: 'scale(1)' }],
        { duration: 1000, easing: 'cubic-bezier(.22,.8,.36,1)', fill: 'both' },
      ),
    )
    const lines = document.querySelectorAll('.hero-title .line')
    lines.forEach((line, index) => {
      heroAnimations.push(
        /** @type {HTMLElement} */ (line).animate(
          [
            { opacity: 0, transform: 'translateY(26px)' },
            { opacity: 1, transform: 'translateY(0)' },
          ],
          { duration: 620, delay: 140 * index, easing: 'cubic-bezier(.22,.8,.36,1)', fill: 'both' },
        ),
      )
    })
  }
} catch {
  // 本机存储不可用:每次都播放入场也不影响内容可见
}

/* ---------- 作品网格 + 大图 dialog ---------- */

const worksGrid = $('#works-grid')
/** @type {HTMLButtonElement[]} */
const workCards = []
WORKS.forEach((work, index) => {
  const card = /** @type {HTMLButtonElement} */ (document.createElement('button'))
  card.type = 'button'
  card.className = 'work-card'
  card.setAttribute('aria-label', `放大查看:${work.title}`)
  card.innerHTML = `<span class='art' style='background-image:url("${work.art}")'></span>
    <figcaption>${work.title}</figcaption>`
  card.addEventListener('click', () => openLightbox(index))
  worksGrid.append(card)
  workCards.push(card)
})

const lightbox = $('#lightbox')
let lightboxSeq = 0
let lightboxTimer = null
/** @type {HTMLElement | null} 打开大图的触发卡片,关闭后焦点返回 */
let lightboxTrigger = null

/** @param {number} index */
function openLightbox(index) {
  const seq = ++lightboxSeq
  if (lightboxTimer) {
    clearTimeout(lightboxTimer)
  }
  lightboxTrigger = workCards[index]
  $('#lightbox-media').style.backgroundImage = `url("${WORKS[index].art}")`
  $('#lightbox-caption').textContent = WORKS[index].caption
  lightbox.hidden = false
  if (reduceMotion.matches) {
    lightbox.classList.add('open')
    $('#lightbox-close').focus()
    return
  }
  // 下一帧再加 open:确保 200ms 进入过渡生效
  requestAnimationFrame(() => {
    if (seq === lightboxSeq) {
      lightbox.classList.add('open')
    }
  })
  $('#lightbox-close').focus()
}

function closeLightbox() {
  const seq = ++lightboxSeq
  if (lightbox.hidden) {
    return
  }
  const finish = () => {
    if (seq !== lightboxSeq) {
      return // 旧关闭回调不能关掉新打开的预览(A19)
    }
    lightbox.hidden = true
    lightboxTrigger?.focus()
    lightboxTrigger = null
  }
  if (reduceMotion.matches) {
    lightbox.classList.remove('open')
    finish()
    return
  }
  lightbox.classList.remove('open') // 150ms 退出过渡
  if (lightboxTimer) {
    clearTimeout(lightboxTimer)
  }
  lightboxTimer = setTimeout(finish, 160)
}

$('#lightbox-close').addEventListener('click', closeLightbox)
$('#lightbox-mask').addEventListener('click', closeLightbox)
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !lightbox.hidden) {
    closeLightbox()
  }
})

/* ---------- 固定导航:滚动背景 + 当前章节提示 ---------- */

const sections = ['scenes', 'works', 'canvas-feature', 'agent']
  .map((id) => document.getElementById(id))
  .filter(Boolean)
const sectionObserver = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) {
        continue
      }
      const id = /** @type {HTMLElement} */ (entry.target).id
      $('#nav-current').textContent =
        { scenes: '场景', works: '作品', 'canvas-feature': '智能画布', agent: '创作助手' }[id] || ''
      for (const link of document.querySelectorAll('.nav-links a')) {
        link.classList.toggle('current', link.getAttribute('href') === `#${id}`)
      }
    }
  },
  { rootMargin: '-30% 0px -60% 0px' },
)
for (const section of sections) {
  sectionObserver.observe(section)
}

const onScroll = () => {
  $('#nav').classList.toggle('scrolled', window.scrollY > 24)
}
window.addEventListener('scroll', onScroll, { passive: true })
onScroll()

/* ---------- 图库与示意图首次进入视口时播放一次入场 ---------- */

const revealObserver = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) {
        continue
      }
      const el = /** @type {HTMLElement} */ (entry.target)
      if (reduceMotion.matches || !el.animate) {
        el.style.opacity = '1'
      } else {
        el.animate(
          [
            { opacity: 0, transform: 'translateY(28px)' },
            { opacity: 1, transform: 'translateY(0)' },
          ],
          { duration: 700, easing: 'cubic-bezier(.22,.8,.36,1)', fill: 'both' },
        )
      }
      revealObserver.unobserve(el)
    }
  },
  { threshold: 0.22 },
)
for (const el of [...workCards, $('#feature-reveal'), ...document.querySelectorAll('.agent-steps li')]) {
  if (el instanceof HTMLElement) {
    if (reduceMotion.matches) {
      el.style.opacity = '1'
    } else {
      el.style.opacity = '0'
      revealObserver.observe(el)
    }
  }
}

/* ---------- 查看示例画布:固定幂等 key,只创建一个本机示例 ---------- */

$('#open-example').addEventListener('click', async () => {
  const btn = /** @type {HTMLButtonElement} */ ($('#open-example'))
  const hint = $('#example-hint')
  btn.disabled = true
  hint.hidden = true
  try {
    const res = await fetch('/api/documents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'landing-example-canvas' },
      body: JSON.stringify({ name: '示例画布(本机示例)' }),
    })
    const body = await res.json().catch(() => ({}))
    if (!res.ok) {
      throw new Error(body.error || `HTTP ${res.status}`)
    }
    location.href = `/canvas?doc=${encodeURIComponent(body.document.id)}`
  } catch (err) {
    hint.textContent = `示例画布创建失败:${err instanceof Error ? err.message : String(err)}(需要本机服务已启动)`
    hint.classList.add('error')
    hint.hidden = false
    btn.disabled = false
  }
})

/* ---------- 离开页面:取消动画与观察,作废未完成的切换 ---------- */

window.addEventListener('pagehide', () => {
  sceneSeq++
  lightboxSeq++
  for (const animation of heroAnimations) {
    animation.cancel()
  }
  heroAnimations = []
  revealObserver.disconnect()
  sectionObserver.disconnect()
})

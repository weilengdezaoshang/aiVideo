// 介绍页交互(PRD §3.1 / A19):
//   - 三场景切换:先 decode,再以请求序号防止快速点击乱序;active class 驱动 650ms 过渡;
//   - 首屏仅首次访问播放 WAAPI 入场;图库/示意图 IntersectionObserver 首次进入播放一次;
//   - 大图 dialog 200ms 进 / 150ms 退,Esc 与关闭共用退出流程,焦点返回触发卡片;
//   - 后台/离开(pagehide)取消动画与观察;减少动态偏好即时取消;
//   - 「查看示例画布」只创建明确标识的本机示例(幂等 key 固定),不调用模型。

/* ---------- 展示素材(PRD §3.1:使用已有 Unsplash 展示素材与实际编辑器截图) ---------- */

const ASSETS = {
  ocean: '/landing/assets/ocean.jpg',
  architecture: '/landing/assets/architecture.jpg',
  flower: '/landing/assets/flower.jpg',
  plant: '/landing/assets/plant.jpg',
  editor: '/landing/assets/editor.jpg',
}

const SCENES = [
  { name: '海面之上', caption: '海面之上 · 远景留白,把环境交还给光(演示素材)', art: ASSETS.ocean },
  { name: '大漠几何', caption: '大漠几何 · 体块与方向光的秩序(演示素材)', art: ASSETS.architecture },
  { name: '花与光', caption: '花与光 · 特写聚焦最具辨识度的细节(演示素材)', art: ASSETS.flower },
]

const WORKS = [
  { title: '演示素材 · 建筑', caption: '展示图:建筑与几何', art: ASSETS.architecture },
  { title: '演示素材 · 花', caption: '展示图:静物花', art: ASSETS.flower },
  { title: '演示素材 · 海', caption: '展示图:海面肌理', art: ASSETS.ocean },
  { title: '演示素材 · 绿', caption: '展示图:绿植静物', art: ASSETS.plant },
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

async function openExample() {
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
}

$('#open-example').addEventListener('click', () => void openExample())
// 点击编辑器截图同样进入示例(PRD §3.1:点击截图或入口进入 example)
$('#editor-shot').addEventListener('click', () => void openExample())

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

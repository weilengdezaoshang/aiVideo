const scenes = {
  ocean: ['海浪之间，发现新的想象', '航拍海浪展示素材'],
  architecture: ['在静谧的空间里，构建想象', '沙漠建筑展示素材'],
  flower: ['把自然的色彩，留在日常', '花卉静物展示素材'],
}
const motionPreference = matchMedia('(prefers-reduced-motion: reduce)')
const sceneImages = [...document.querySelectorAll('[data-hero-scene]')]
const sceneButtons = [...document.querySelectorAll('[data-scene]')]
const seenSections = new WeakSet()
let sceneRequest = 0
let observer = null
let introEntered = false
const motionAllowed = () => !motionPreference.matches && !intro.hidden && !document.hidden
const easeOut = 'cubic-bezier(.22,1,.36,1)'
const landingNav = document.querySelector('.landing-nav')
const chapterLinks = [...document.querySelectorAll('.landing-links a[href^="#create-"]')]
let navFrame = 0
function updateChapterNavigation() {
  navFrame = 0
  if (intro.hidden) {
    return
  }
  landingNav.classList.toggle('is-scrolled', window.scrollY > 24)
  let active = null
  chapterLinks.forEach((link) => {
    const section = document.querySelector(link.getAttribute('href'))
    if (section.getBoundingClientRect().top <= landingNav.offsetHeight + 140) {
      active = link
    }
  })
  chapterLinks.forEach((link) => {
    if (link === active) {
      link.setAttribute('aria-current', 'location')
    } else {
      link.removeAttribute('aria-current')
    }
  })
}
window.addEventListener(
  'scroll',
  () => {
    if (!intro.hidden && !navFrame) {
      navFrame = requestAnimationFrame(updateChapterNavigation)
    }
  },
  { passive: true },
)
window.addEventListener('resize', updateChapterNavigation)
window.addEventListener('landing-visibility', updateChapterNavigation)
updateChapterNavigation()

sceneButtons.forEach((button) => {
  button.onclick = async () => {
    const key = button.dataset.scene
    const nextImage = sceneImages.find((img) => img.dataset.heroScene === key)
    const requestId = ++sceneRequest
    if (!nextImage) {
      return
    }
    try {
      await nextImage.decode()
    } catch {
      if (requestId === sceneRequest && !intro.hidden) {
        document.getElementById('landing-scene-copy').textContent =
          '展示素材暂时无法加载，请切换其他作品。'
      }
      return
    }
    if (requestId !== sceneRequest || intro.hidden || document.hidden) {
      return
    }
    sceneImages.forEach((img) => {
      const active = img === nextImage
      img.classList.toggle('is-scene-active', active)
      img.setAttribute('aria-hidden', String(!active))
    })
    document.getElementById('landing-scene-copy').textContent = scenes[key][0]
    nextImage.alt = scenes[key][1]
    sceneButtons.forEach((other) => other.setAttribute('aria-pressed', String(other === button)))
  }
})

function stopLandingMotion() {
  if (navFrame) {
    cancelAnimationFrame(navFrame)
  }
  navFrame = 0
  sceneRequest++
  intro.dataset.motionPaused = 'true'
  observer?.disconnect()
  observer = null
  intro.getAnimations({ subtree: true }).forEach((animation) => animation.cancel())
}
function startLandingMotion() {
  if (!motionAllowed()) {
    stopLandingMotion()
    return
  }
  intro.dataset.motionPaused = 'false'
  if (!introEntered) {
    introEntered = true
    const activeImage = sceneImages.find((img) => img.classList.contains('is-scene-active'))
    activeImage?.animate([{ transform: 'scale(1.035)' }, { transform: 'scale(1)' }], {
      duration: 1000,
      easing: easeOut,
    })
    document.querySelectorAll('.landing-title h1 span').forEach((line, index) => {
      line.animate(
        [
          { opacity: 0.35, transform: 'translateY(16px)' },
          { opacity: 1, transform: 'translateY(0)' },
        ],
        { duration: 620, delay: index * 80, easing: easeOut },
      )
    })
  }
  if (observer || !('IntersectionObserver' in window)) {
    return
  }
  observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting || !motionAllowed() || seenSections.has(entry.target)) {
          return
        }
        seenSections.add(entry.target)
        observer?.unobserve(entry.target)
        if (entry.target.classList.contains('landing-art-grid')) {
          entry.target.querySelectorAll('.landing-art').forEach((art, index) => {
            art.animate(
              [
                { opacity: 0.45, transform: 'translateY(18px)' },
                { opacity: 1, transform: 'translateY(0)' },
              ],
              { duration: 480, delay: index * 55, easing: easeOut },
            )
          })
        } else if (entry.target.matches('.landing-section-title,.landing-agent .landing-copy')) {
          entry.target.animate(
            [
              { opacity: 0.35, transform: 'translateY(12px)' },
              { opacity: 1, transform: 'translateY(0)' },
            ],
            { duration: 440, easing: easeOut },
          )
        } else {
          entry.target.animate(
            [
              { opacity: 0.5, transform: 'translateY(24px) scale(.99)' },
              { opacity: 1, transform: 'translateY(0) scale(1)' },
            ],
            { duration: 620, easing: easeOut },
          )
        }
      })
    },
    { threshold: 0.15 },
  )
  document
    .querySelectorAll(
      '.landing-section-title,.landing-art-grid,.landing-editor-figure,.landing-agent .landing-copy',
    )
    .forEach((target) => {
      if (!seenSections.has(target)) {
        observer.observe(target)
      }
    })
}
window.addEventListener('landing-visibility', (event) =>
  event.detail ? startLandingMotion() : stopLandingMotion(),
)
document.addEventListener('visibilitychange', () =>
  document.hidden ? stopLandingMotion() : startLandingMotion(),
)
window.addEventListener('pagehide', stopLandingMotion)
window.addEventListener('pageshow', startLandingMotion)
motionPreference.addEventListener('change', () => {
  stopLandingMotion()
  product.getAnimations().forEach((animation) => animation.cancel())
  // Enabling motion later does not replay the initial presentation.
  introEntered = true
  startLandingMotion()
})
startLandingMotion()

const lightbox = document.getElementById('art-lightbox')
let galleryTrigger = null
let lightboxSequence = 0
function closeArtwork() {
  if (!lightbox.open || lightbox.classList.contains('is-closing')) {
    return
  }
  if (!motionAllowed()) {
    lightbox.close()
    return
  }
  const sequence = ++lightboxSequence
  const visual = getComputedStyle(lightbox)
  const from = { opacity: visual.opacity, transform: visual.transform }
  lightbox.getAnimations().forEach((animation) => animation.cancel())
  lightbox.classList.add('is-closing')
  const exit = lightbox.animate([from, { opacity: 0, transform: 'scale(.98)' }], {
    duration: 150,
    easing: easeOut,
    fill: 'forwards',
  })
  exit.finished
    .catch(() => {})
    .then(() => {
      if (sequence === lightboxSequence && lightbox.open) {
        lightbox.close()
      }
    })
}
document.querySelectorAll('[data-art]').forEach((button) => {
  button.onclick = () => {
    lightboxSequence++
    lightbox.classList.remove('is-closing')
    lightbox.getAnimations().forEach((animation) => animation.cancel())
    galleryTrigger = button
    document.getElementById('art-lightbox-image').src = VISUAL_ASSETS[button.dataset.art]
    document.getElementById('art-lightbox-image').alt = button.getAttribute('aria-label')
    document.getElementById('art-lightbox-title').textContent = button.dataset.title
    lightbox.showModal()
    if (motionAllowed()) {
      lightbox.animate(
        [
          { opacity: 0, transform: 'scale(.98)' },
          { opacity: 1, transform: 'scale(1)' },
        ],
        { duration: 200, easing: easeOut },
      )
    }
  }
})
document.getElementById('close-art').onclick = closeArtwork
lightbox.addEventListener('cancel', (event) => {
  event.preventDefault()
  closeArtwork()
})
lightbox.addEventListener('close', () => {
  lightboxSequence++
  lightbox.classList.remove('is-closing')
  lightbox.getAnimations().forEach((animation) => animation.cancel())
  if (current === 'intro') {
    galleryTrigger?.focus()
  }
})

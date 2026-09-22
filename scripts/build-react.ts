import { build, context, type BuildOptions } from 'esbuild'

const watching = process.argv.includes('--watch')
const options: BuildOptions = {
  entryPoints: [
    { in: 'apps/web/workspace/app.tsx', out: 'workspace/app' },
    { in: 'apps/web/landing/main.tsx', out: 'landing/app' },
    { in: 'apps/web/evaluation/app.tsx', out: 'evaluation/app' },
    { in: 'apps/web/canvas-next/app.tsx', out: 'canvas-next/app' },
  ],
  bundle: true,
  format: 'esm',
  minify: !watching,
  sourcemap: true,
  outdir: '.web-build',
  logLevel: 'info',
}
if (watching) {
  const compiler = await context(options)
  await compiler.watch()
  const stop = async () => {
    await compiler.dispose()
    process.exit(0)
  }
  process.once('SIGINT', () => void stop())
  process.once('SIGTERM', () => void stop())
} else {
  await build(options)
}

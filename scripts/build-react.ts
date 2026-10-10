import { build, context, type BuildOptions } from 'esbuild'
import { writeFile } from 'node:fs/promises'

const watching = process.argv.includes('--watch')
const options: BuildOptions = {
  entryPoints: [
    { in: 'apps/web/workspace/app.tsx', out: 'workspace/app' },
    { in: 'apps/web/landing/main.tsx', out: 'landing/app' },
    { in: 'apps/web/evaluation/app.tsx', out: 'evaluation/app' },
    { in: 'apps/web/canvas/app.tsx', out: 'canvas/app' },
  ],
  bundle: true,
  format: 'esm',
  minify: !watching,
  sourcemap: true,
  outdir: process.env.BUNDLE_OUTDIR || '.web-build',
  metafile: Boolean(process.env.BUNDLE_METAFILE),
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
  const result = await build(options)
  if (process.env.BUNDLE_METAFILE && result.metafile) {
    await writeFile(process.env.BUNDLE_METAFILE, JSON.stringify(result.metafile, null, 2))
  }
}

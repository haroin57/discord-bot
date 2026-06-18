import { defineConfig, type Plugin, type Connect } from 'vite'
import react from '@vitejs/plugin-react'
import { readFile, stat } from 'node:fs/promises'
import { resolve } from 'node:path'

const KANAE_DAILY_PATH = resolve(
  __dirname,
  '../../.state/kanae_daily.json',
)

const apiHandler: Connect.SimpleHandleFunction = async (_req, res) => {
  try {
    const [raw, st] = await Promise.all([
      readFile(KANAE_DAILY_PATH, 'utf-8'),
      stat(KANAE_DAILY_PATH),
    ])
    res.setHeader('Content-Type', 'application/json; charset=utf-8')
    res.setHeader('Cache-Control', 'no-store')
    res.setHeader('X-Source-Mtime', st.mtime.toISOString())
    res.end(raw)
  } catch (err) {
    res.statusCode = 500
    res.setHeader('Content-Type', 'application/json; charset=utf-8')
    res.end(
      JSON.stringify({
        error: 'read_failed',
        message: err instanceof Error ? err.message : String(err),
        path: KANAE_DAILY_PATH,
      }),
    )
  }
}

function kanaeDailyApi(): Plugin {
  return {
    name: 'kanae-daily-api',
    configureServer(server) {
      server.middlewares.use('/api/kanae-daily', apiHandler)
    },
    configurePreviewServer(server) {
      server.middlewares.use('/api/kanae-daily', apiHandler)
    },
  }
}

export default defineConfig({
  plugins: [react(), kanaeDailyApi()],
  server: {
    host: '127.0.0.1',
    port: 5173,
  },
  preview: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
  },
})

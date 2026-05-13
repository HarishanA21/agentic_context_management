/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [
      // Pin to 127.0.0.1, not `localhost`. Node 17+ resolves `localhost` to
      // `::1` (IPv6) first on macOS, and our uvicorn only listens on IPv4 —
      // so the proxy gets ECONNREFUSED on the IPv6 attempt and surfaces it
      // as `socket hang up` to the browser.
      { source: '/api/:path*', destination: 'http://127.0.0.1:8000/:path*' },
    ]
  },
}

export default nextConfig

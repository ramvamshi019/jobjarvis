/** @type {import('next').NextConfig} */
const nextConfig = {
  // Required for the Docker multi-stage build: produces .next/standalone/
  // so the runner stage can copy a self-contained Node server.
  output: "standalone",

  // Proxy all /api/* calls to the FastAPI backend.
  // Override BACKEND_URL in production (e.g. https://api.jobjarvis.io).
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.BACKEND_URL || "http://localhost:8000"}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;

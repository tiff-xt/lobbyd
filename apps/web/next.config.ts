import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The generated API types live in a workspace package as plain TypeScript.
  transpilePackages: ["@lobbyd/api-client"],
  // A small self-contained server for the Docker image.
  output: "standalone",
  // The workspace root, so the standalone build can find hoisted packages.
  outputFileTracingRoot: path.join(import.meta.dirname, "../.."),
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;

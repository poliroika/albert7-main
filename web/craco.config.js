// Patch require before react-scripts loads fork-ts-checker (schema-utils/ajv breaks on Node 20+/25).
const path = require("path");
const Module = require("module");
const _forkTsStub = path.join(__dirname, "scripts", "fork-ts-checker-stub.cjs");
const _origRequire = Module.prototype.require;
Module.prototype.require = function (id) {
  if (id === "fork-ts-checker-webpack-plugin") {
    return _origRequire.call(this, _forkTsStub);
  }
  return _origRequire.apply(this, arguments);
};

require("dotenv").config();

// Check if we're in development/preview mode (not production build)
// Craco sets NODE_ENV=development for start, NODE_ENV=production for build
const isDevServer = process.env.NODE_ENV !== "production";

// Environment variable overrides
const config = {
  enableHealthCheck: process.env.ENABLE_HEALTH_CHECK === "true",
};

// Conditionally load health check modules only if enabled
let WebpackHealthPlugin;
let setupHealthEndpoints;
let healthPluginInstance;

if (config.enableHealthCheck) {
  WebpackHealthPlugin = require("./plugins/health-check/webpack-health-plugin");
  setupHealthEndpoints = require("./plugins/health-check/health-endpoints");
  healthPluginInstance = new WebpackHealthPlugin();
}

/** Dev: CRA serves UI on :3000, web_bridge API on :8765 — same-origin `/api` must be proxied. */
function applyApiProxyToDevServer(devServerConfig) {
  if (!devServerConfig || typeof devServerConfig !== "object") {
    return;
  }
  const apiTarget = process.env.REACT_APP_DEV_API_PROXY || "http://127.0.0.1:8765";
  const apiProxyEntry = { target: apiTarget, changeOrigin: true };
  if (!devServerConfig.proxy) {
    devServerConfig.proxy = { "/api": apiProxyEntry };
  } else if (Array.isArray(devServerConfig.proxy)) {
    devServerConfig.proxy = [{ context: ["/api"], ...apiProxyEntry }, ...devServerConfig.proxy];
  } else {
    devServerConfig.proxy = { ...devServerConfig.proxy, "/api": apiProxyEntry };
  }
}

let webpackConfig = {
  eslint: {
    configure: {
      extends: ["plugin:react-hooks/recommended"],
      rules: {
        "react-hooks/rules-of-hooks": "error",
        "react-hooks/exhaustive-deps": "warn",
      },
    },
  },
  webpack: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
    configure: (webpackConfig) => {

      // Add ignored patterns to reduce watched directories
        webpackConfig.watchOptions = {
          ...webpackConfig.watchOptions,
          ignored: [
            '**/node_modules/**',
            '**/.git/**',
            '**/build/**',
            '**/dist/**',
            '**/coverage/**',
            '**/public/**',
        ],
      };

      // Add health check plugin to webpack if enabled
      if (config.enableHealthCheck && healthPluginInstance) {
        webpackConfig.plugins.push(healthPluginInstance);
      }
      return webpackConfig;
    },
  },
};

webpackConfig.devServer = (devServerConfig) => {
  applyApiProxyToDevServer(devServerConfig);

  // Add health check endpoints if enabled
  if (config.enableHealthCheck && setupHealthEndpoints && healthPluginInstance) {
    const originalSetupMiddlewares = devServerConfig.setupMiddlewares;

    devServerConfig.setupMiddlewares = (middlewares, devServer) => {
      // Call original setup if exists
      if (originalSetupMiddlewares) {
        middlewares = originalSetupMiddlewares(middlewares, devServer);
      }

      // Setup health endpoints
      setupHealthEndpoints(devServer, healthPluginInstance);

      return middlewares;
    };
  }

  return devServerConfig;
};

// Wrap with visual edits (automatically adds babel plugin, dev server, and overlay in dev mode)
if (isDevServer) {
  try {
    const { withVisualEdits } = require("@emergentbase/visual-edits/craco");
    webpackConfig = withVisualEdits(webpackConfig);
  } catch (err) {
    if (err.code === 'MODULE_NOT_FOUND' && err.message.includes('@emergentbase/visual-edits/craco')) {
      console.warn(
        "[visual-edits] @emergentbase/visual-edits not installed — visual editing disabled."
      );
    } else {
      throw err;
    }
  }
  const previousDevServer = webpackConfig.devServer;
  webpackConfig.devServer = (devServerConfig, ...rest) => {
    const out =
      typeof previousDevServer === "function"
        ? previousDevServer(devServerConfig, ...rest)
        : devServerConfig;
    applyApiProxyToDevServer(out);
    return out;
  };
}

module.exports = webpackConfig;

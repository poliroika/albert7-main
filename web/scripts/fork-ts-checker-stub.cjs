"use strict";

/** No-op replacement for fork-ts-checker-webpack-plugin (avoids ajv 6/8 + schema-utils crashes on Node 20+). */
class ForkTsCheckerWebpackPlugin {
  constructor(_opts) {}

  apply(_compiler) {}
}

module.exports = ForkTsCheckerWebpackPlugin;

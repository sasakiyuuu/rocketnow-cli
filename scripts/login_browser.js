#!/usr/bin/env node
"use strict";

const path = require("node:path");
process.env.ROCKETNOW_PLAYWRIGHT_ROOT ||= path.resolve(__dirname, "..");
require("../src/rocketnow_cli/assets/login_browser.js");

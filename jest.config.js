module.exports = {
  testEnvironment: "jsdom",
  testMatch: ["**/tests/js/**/*.test.js"],
  collectCoverageFrom: [
    "static/**/*.js",
    "!static/app.js",
  ],
  coverageThreshold: {
    global: {
      // Current-floor gates; raise as browser-flow coverage expands.
      branches: 45,
      functions: 60,
      lines: 65,
      statements: 60,
    },
  },
};

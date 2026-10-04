// Lint rules for the dashboard's scripts. Run: npm install && npx eslint static
// The recommended rule set, as ES modules in the browser. Rules turned down are
// explained beside them.
import js from "@eslint/js";
import globals from "globals";

export default [
  js.configs.recommended,
  {
    files: ["static/js/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...globals.browser },
    },
    rules: {
      // catch blocks that deliberately ignore a failure (storage, a missing element)
      "no-empty": ["error", { allowEmptyCatch: true }],
      // unused parameters are kept where they document a callback's shape
      "no-unused-vars": ["error", { args: "none", caughtErrors: "none" }],
    },
  },
  {
    files: ["static/screen.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.browser },
    },
    rules: {
      "no-empty": ["error", { allowEmptyCatch: true }],
      "no-unused-vars": ["error", { args: "none", caughtErrors: "none" }],
    },
  },
];

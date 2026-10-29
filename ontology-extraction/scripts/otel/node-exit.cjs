// Loaded after OpenTelemetry's register hook by otel_setup.py run.
// That hook listens for SIGTERM to send the last spans, which replaces Node's default
// (exit), so a service that used to stop on SIGTERM would keep running. Restore the
// default once the spans have had a moment to go, unless the application handles
// SIGTERM itself.
process.once("SIGTERM", () => {
  if (process.listenerCount("SIGTERM") > 1) return;
  setTimeout(() => process.exit(143), 1500);
});

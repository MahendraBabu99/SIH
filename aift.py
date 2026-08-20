# ==============================================================================
# AIFT - AI Forensic Triage System (Student Project Submission)
# Main App Launcher File
# ==============================================================================

import sys
import threading
import webbrowser

from runtime_compat import UnsupportedPythonVersionError, assert_supported_python_version


def main():
    # Step 1: Check Python version
    assert_supported_python_version()

    from app import create_app
    from app.utils.config import ConfigurationError, load_config

    # Step 2: Try to load the config file
    try:
        config = load_config()
    except ConfigurationError as exc:
        print("ERROR: Cannot start AIFT - invalid configuration:")
        for e in exc.errors:
            print("  - " + str(e))
        sys.exit(1)

    # Step 3: Get server port and host from config
    server_config = config.get("server", {})
    host = server_config.get("host", "127.0.0.1")
    port = int(server_config.get("port", 5000))

    # Step 4: Create flask app
    app = create_app(config=config)
    url = "http://" + str(host) + ":" + str(port)

    # Helper function to open browser automatically
    def _open_browser():
        try:
            webbrowser.open(url)
        except Exception:
            pass  # ignore if browser doesn't open

    # Launch browser after 1 second delay
    browser_timer = threading.Timer(1.0, _open_browser)
    browser_timer.daemon = True
    browser_timer.start()
    
    # Step 5: Start server!
    try:
        print("Starting AIFT server on " + url)
        app.run(host=host, port=port, debug=False, use_reloader=False)
    finally:
        browser_timer.cancel()


# Run main function
if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Fatal error:", error)
        sys.exit(1)



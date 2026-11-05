"""Munim: an accounts-payable agent that pays real invoices exactly once and stops the fake ones."""

try:
    # Use the operating system's certificate store, so TLS works behind campus or
    # antivirus proxies that Python's bundled certificates do not know about.
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

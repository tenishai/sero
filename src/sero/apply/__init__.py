"""The SDK apply layer (D3b): write rendered config to a live OpenBao.

The rendering layer (``sero.config``) produces plain data: policy HCL, auth
methods, namespaces. This layer turns that data into an ordered plan of writes
against the OpenBao API and, in a later step, executes it. Planning is pure and
unit-tested here; the execution wrapper that talks to OpenBao is separate and
thin, mirroring ``sero.install``.
"""

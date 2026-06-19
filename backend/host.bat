@echo off
:: Windows launcher for the native messaging host.
:: Chrome's NativeMessagingHosts manifest points to this file.
:: It simply invokes Python with host.py in the same directory.
python "%~dp0host.py" %*

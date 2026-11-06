@echo off
rem Copy this file to email_settings.bat and fill in your own Gmail address and app password.
rem run.bat loads it automatically, so password-reset e-mails are really sent.
rem Gmail app password: Google Account > Security > 2-Step Verification (on) > App passwords.
set EMAIL_HOST=smtp.gmail.com
set EMAIL_PORT=587
set EMAIL_HOST_USER=youraddress@gmail.com
set EMAIL_HOST_PASSWORD=abcdabcdabcdabcd
set DEFAULT_FROM_EMAIL=AgriMarket ^<youraddress@gmail.com^>
rem Optional: your Paystack TEST secret key, used when none is saved in Settings (survives a new database).
set PAYSTACK_SECRET_KEY=

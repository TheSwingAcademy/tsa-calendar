# TSA Calendar Server

Small Flask server for the TSA trading dashboard.

## Endpoints

- `/` health/info
- `/calendar` cached Forex Factory calendar JSON
- `/health` status check

## Render deployment

1. Upload these files to a GitHub repository.
2. Connect the repository to Render as a Web Service.
3. Render will use `render.yaml`.
4. Open `/calendar` on the Render URL.

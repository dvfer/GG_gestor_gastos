source .env
gcloud run services update gg-parser \
    --update-env-vars API_KEY=$API_KEY,SPREADSHEET_ID=$SPREADSHEET_ID,SHEET_NAME=$SHEET_NAME \
    --region us-central1
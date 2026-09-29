# API tests (Postman)

45 requests and 120 checks covering every endpoint, including a **real** Gemini call.

## Run from the terminal

```
cd skincare-advisor-backend
python scripts/make_postman_env.py
npx newman run postman/skincare-advisor.postman_collection.json -e postman/local.postman_environment.json
```

The backend must be running on port 8000.

## Run in the Postman app

1. Run `python scripts/make_postman_env.py`. You need to do this before every run: it creates fresh test users, and the tokens last 2 hours.
2. In Postman, **Import** both `skincare-advisor.postman_collection.json` and `local.postman_environment.json`.
3. Select the **Skincare Advisor - local** environment (top right).
4. Right-click the collection, choose **Run collection**, then **Run**. Keep the folders in their order.

## What it creates

The run creates three throwaway users: an adult, a "referral" user, and an under-13 user. The last folder deletes all three again. The scan sends a plain grey image, not a face, so a correct result is `IMAGE_UNUSABLE` and the scan is not used up.

`local.postman_environment.json` contains working login tokens. It is git-ignored; don't commit or share it.

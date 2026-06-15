name: ABC Restock Watch

on:
  schedule:
    # Every 15 minutes. Cron is in UTC. GitHub may delay scheduled runs
    # during peak load, so don't expect perfectly even spacing.
    - cron: "*/15 * * * *"
  workflow_dispatch: {}   # lets you trigger a run manually from the Actions tab

# Allow the workflow to push the updated state.json back to the repo.
permissions:
  contents: write

# Prevent overlapping runs from racing on the state commit.
concurrency:
  group: abc-restock
  cancel-in-progress: false

jobs:
  watch:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install requests

      - name: Run watcher
        env:
          # These two are NOT sensitive (public store IDs / product codes).
          # Reading them from Variables (not Secrets) means GitHub won't mask
          # digits in the logs, so your bottle counts print as real numbers.
          WATCH_PRODUCTS: ${{ vars.WATCH_PRODUCTS }}
          STORE_NUMBERS: ${{ vars.STORE_NUMBERS }}
          SMS_TO: ${{ secrets.SMS_TO }}
          SMTP_USER: ${{ secrets.SMTP_USER }}
          SMTP_PASS: ${{ secrets.SMTP_PASS }}
          # SMTP server — defaults to Gmail in the script. For iCloud set
          # SMTP_HOST=smtp.mail.me.com and SMTP_PORT=587 as secrets.
          SMTP_HOST: ${{ secrets.SMTP_HOST }}
          SMTP_PORT: ${{ secrets.SMTP_PORT }}
          # Set ONLY_WATCHED secret to "0" to also track nearby stores the
          # API volunteers; default (unset) tracks only STORE_NUMBERS.
          ONLY_WATCHED: ${{ secrets.ONLY_WATCHED }}
          # Set DEBUG_JSON secret to "1" temporarily to dump raw API JSON.
          DEBUG_JSON: ${{ secrets.DEBUG_JSON }}
          # Set TEST_SMS secret to "1" to send one test text and exit.
          TEST_SMS: ${{ secrets.TEST_SMS }}
        run: python abc_restock.py

      - name: Commit updated state
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add state.json
          if git diff --staged --quiet; then
            echo "No state change."
          else
            git commit -m "Update restock state [skip ci]"
            git push
          fi


if __name__ == "__main__":
    main()

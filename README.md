# Wallstreet Reviews, X API Check

גרסת בדיקה ראשונה לפרויקט סקירות וול סטריט.

המטרה בשלב הזה: לבדוק ש-GitHub מצליח למשוך פוסטים מחמשת חשבונות X שהוגדרו.

## קבצים חשובים

- `accounts.txt`, רשימת חשבונות X למעקב.
- `scripts/check_x_api.py`, סקריפט בדיקה שמושך פוסטים מה-X API.
- `.github/workflows/check-x-api.yml`, פעולה של GitHub Actions להרצה ידנית.
- `requirements.txt`, ספריות Python נדרשות.

## Secret נדרש

ב-GitHub יש להוסיף Secret בשם:

```text
X_BEARER_TOKEN
```

הנתיב:

```text
Settings → Secrets and variables → Actions → New repository secret
```

לא להעלות API key לקבצים בריפו.

## איך מריצים

1. להיכנס ל-Repository.
2. ללחוץ על `Actions`.
3. לבחור `Check X API`.
4. ללחוץ `Run workflow`.
5. לפתוח את תוצאת ההרצה ולבדוק אם נוצר קובץ בתיקיית `output`.

## מה הבדיקה עושה

- קוראת את רשימת החשבונות מתוך `accounts.txt`.
- ממירה כל username ל-user id דרך X API.
- מושכת עד 10 פוסטים אחרונים לכל חשבון.
- שומרת קובץ JSON בתיקיית `output`.


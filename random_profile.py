"""Random profile generator: password, full name, age."""
from __future__ import annotations

import random
import secrets
import string


# First names + last names — pool đủ rộng, common names US/EU
_FIRST_NAMES = (
    "Aaron", "Adam", "Alex", "Alexander", "Andrew", "Anthony", "Asher", "Austin",
    "Benjamin", "Blake", "Brandon", "Brian", "Caleb", "Cameron", "Carter", "Charles",
    "Christian", "Christopher", "Cody", "Cole", "Colton", "Connor", "Daniel", "David",
    "Dean", "Dominic", "Dylan", "Easton", "Edward", "Elijah", "Eric", "Ethan",
    "Evan", "Felix", "Gabriel", "Gavin", "George", "Grayson", "Henry", "Hudson",
    "Hunter", "Ian", "Isaac", "Isaiah", "Jack", "Jackson", "Jacob", "James",
    "Jason", "Jeremy", "Joel", "John", "Jonathan", "Jordan", "Joseph", "Joshua",
    "Julian", "Justin", "Kevin", "Kyle", "Landon", "Leo", "Levi", "Liam",
    "Logan", "Lucas", "Luke", "Mark", "Matthew", "Max", "Mason", "Michael",
    "Miles", "Nathan", "Nicholas", "Noah", "Oliver", "Owen", "Parker", "Patrick",
    "Paul", "Peter", "Philip", "Quinn", "Reid", "Robert", "Ryan", "Samuel",
    "Sean", "Sebastian", "Simon", "Steven", "Thomas", "Timothy", "Tyler", "Vincent",
    "Wesley", "William", "Wyatt", "Xavier", "Zachary", "Zane",
    "Ava", "Amelia", "Aria", "Aurora", "Avery", "Bella", "Brooklyn", "Camila",
    "Charlotte", "Chloe", "Claire", "Eleanor", "Elena", "Eliana", "Elizabeth",
    "Ella", "Ellie", "Emily", "Emma", "Evelyn", "Gianna", "Grace", "Hannah",
    "Harper", "Hazel", "Isabella", "Isla", "Ivy", "Julia", "Kennedy", "Layla",
    "Leah", "Lila", "Lily", "Lucy", "Luna", "Madison", "Mia", "Mila",
    "Naomi", "Natalie", "Nora", "Olivia", "Penelope", "Riley", "Ruby", "Sadie",
    "Sarah", "Savannah", "Scarlett", "Sofia", "Sophia", "Stella", "Valentina",
    "Victoria", "Violet", "Willow", "Zoe",
)

_LAST_NAMES = (
    "Adams", "Allen", "Anderson", "Bailey", "Baker", "Barnes", "Bell", "Bennett",
    "Brooks", "Brown", "Bryant", "Butler", "Campbell", "Carter", "Clark", "Coleman",
    "Collins", "Cook", "Cooper", "Cox", "Davis", "Diaz", "Edwards", "Evans",
    "Fisher", "Flores", "Foster", "Garcia", "Gomez", "Gonzalez", "Gray", "Green",
    "Griffin", "Hall", "Harris", "Hayes", "Henderson", "Hernandez", "Hill", "Howard",
    "Hughes", "Jackson", "James", "Jenkins", "Johnson", "Jones", "Kelly", "King",
    "Lee", "Lewis", "Long", "Lopez", "Martin", "Martinez", "Miller", "Mitchell",
    "Moore", "Morgan", "Morris", "Murphy", "Nelson", "Nguyen", "Parker", "Perez",
    "Perry", "Peterson", "Phillips", "Powell", "Price", "Ramirez", "Reed", "Reyes",
    "Richardson", "Rivera", "Roberts", "Robinson", "Rodriguez", "Rogers", "Ross",
    "Russell", "Sanchez", "Sanders", "Scott", "Simmons", "Smith", "Stewart", "Sullivan",
    "Taylor", "Thomas", "Thompson", "Torres", "Turner", "Walker", "Ward", "Watson",
    "White", "Williams", "Wilson", "Wood", "Wright", "Young",
)


def random_full_name() -> str:
    """Random first + last name (Title Case)."""
    return f"{secrets.choice(_FIRST_NAMES)} {secrets.choice(_LAST_NAMES)}"


def random_age(*, low: int = 19, high: int = 30) -> int:
    """Random age trong khoảng [low, high]."""
    return secrets.randbelow(high - low + 1) + low


def random_password(*, length: int = 12) -> str:
    """Random password 12 ký tự:
        - Bắt đầu bằng 1 chữ HOA.
        - Có chữ thường + số.
        - Kết thúc bằng @ hoặc # (ký tự ngẫu nhiên trong "@#").

    Format: [A-Z][a-z0-9]*8 + 1 chữ + 1 số + [@#]
    Tổng 12 ký tự.
    """
    if length < 4:
        raise ValueError("password length tối thiểu là 4")

    upper = secrets.choice(string.ascii_uppercase)
    end = secrets.choice("@#")

    # Phần giữa (length - 2) ký tự — đảm bảo có ít nhất 1 lower + 1 digit
    middle_len = length - 2
    if middle_len < 2:
        middle = secrets.choice(string.ascii_lowercase) + secrets.choice(string.digits)
    else:
        # Lấy 1 lower + 1 digit + (middle_len - 2) ký tự random từ alphanumeric
        chars = [
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.digits),
        ]
        pool = string.ascii_lowercase + string.digits
        chars.extend(secrets.choice(pool) for _ in range(middle_len - 2))
        # Shuffle phần middle để vị trí lower/digit không cố định
        random.shuffle(chars)
        middle = "".join(chars)

    return f"{upper}{middle}{end}"


# Ngày sinh mặc định (yêu cầu user): năm cố định 1999, NGÀY và THÁNG random
# trong [1, 12]. Giới hạn ≤ 12 để "swap-safe" — nếu form/locale hiểu nhầm thứ
# tự DD/MM ↔ MM/DD thì giá trị vẫn luôn hợp lệ (không bao giờ sinh ngày/tháng
# > 12). ``age`` được compute lại từ birthdate để DB/log nhất quán.
DEFAULT_BIRTH_YEAR = 1999


def _default_birthdate() -> str:
    """Birthdate mặc định ISO ``YYYY-MM-DD``: năm 1999, ngày+tháng random [1,12]."""
    month = secrets.randbelow(12) + 1   # 1..12
    day = secrets.randbelow(12) + 1     # 1..12 (≤12 → swap-safe với tháng)
    return f"{DEFAULT_BIRTH_YEAR:04d}-{month:02d}-{day:02d}"


def _age_from_birthdate(birthdate: str) -> int:
    """Tuổi tròn tính từ birthdate ISO ``YYYY-MM-DD`` (UI rule: year diff, đã
    qua sinh nhật năm nay chưa)."""
    from datetime import datetime
    y, m, d = (int(x) for x in birthdate.split("-"))
    today = datetime.utcnow()
    return today.year - y - ((today.month, today.day) < (m, d))


def random_profile() -> dict:
    """Combo profile: name + password random, birthdate năm 1999 + ngày/tháng
    random ≤ 12 (swap-safe)."""
    name = random_full_name()
    password = random_password()
    birthdate = _default_birthdate()
    return {
        "name": name,
        "age": _age_from_birthdate(birthdate),
        "password": password,
        "birthdate": birthdate,
    }


# ─────────────────────────────────────────────────────────────────────
# Locale-aware profile (anti-ban: tên khớp proxy country)
# ─────────────────────────────────────────────────────────────────────
#
# Trace tay (HAR `web_record_20260625-120705_manual`) cho thấy server cross-check
# IP country (proxy) ↔ tên submit ↔ địa chỉ → profile US "Aaron Smith" submit
# qua proxy India = anti-fraud signal. Helper này chọn name pool theo locale.
#
# Mapping (mở rộng được khi có thêm pool):
#   en-IN, hi-* → India pool (random_india_profile, password+birthdate giữ)
#   en-US, en-GB, en-AU, en → US/EU pool (random_profile)
#   en-* khác   → US/EU pool (default)
#   không khớp  → US/EU pool (safe fallback)
#
# Pool mở rộng (Phase 5):
#   zh-CN, zh-* → CN pool (chưa có)
#   pt-BR       → BR pool (chưa có)
#   …


def random_profile_for_locale(locale: str | None) -> dict:
    """Random profile theo locale string.

    Trả về dict cùng shape với ``random_profile()`` (name, age, password,
    birthdate) — KHÔNG bao gồm các field địa chỉ India (caller dùng
    ``random_india_profile()`` trực tiếp khi cần billing form).

    Args:
        locale: locale BCP-47 (vd "en-IN", "en-US"). None → default US.
    """
    loc = (locale or "").lower().strip()

    # India: en-IN hoặc bất kỳ ngôn ngữ Ấn nào (hi, ta, te, bn, ...)
    if loc.startswith("en-in") or loc.startswith("hi") or loc.startswith("bn-in") \
            or loc.startswith("ta-in") or loc.startswith("te-in"):
        full = random_india_profile()
        # Strip phần địa chỉ — caller cần billing thì gọi random_india_profile() trực tiếp.
        return {
            "name": full["name"],
            "age": full["age"],
            "password": full["password"],
            "birthdate": full["birthdate"],
        }

    # US/EU pool — bao gồm en-US, en-GB, en-AU, en-CA, en, hoặc fallback unknown.
    return random_profile()


# ─────────────────────────────────────────────────────────────────────
# India profile / billing generator
# ─────────────────────────────────────────────────────────────────────

# Tên Ấn Độ phổ biến (mix nam/nữ) — dùng cho profile + billing name.
_IN_FIRST_NAMES = (
    "Aarav", "Aditya", "Arjun", "Ayaan", "Dhruv", "Ishaan", "Kabir", "Karan",
    "Krishna", "Reyansh", "Rohan", "Rudra", "Sai", "Shaurya", "Vihaan", "Vivaan",
    "Aanya", "Aadhya", "Ananya", "Anika", "Diya", "Ira", "Kavya", "Myra",
    "Navya", "Neha", "Pari", "Pooja", "Priya", "Riya", "Saanvi", "Tara",
)

_IN_LAST_NAMES = (
    "Sharma", "Verma", "Gupta", "Singh", "Kumar", "Patel", "Reddy", "Nair",
    "Iyer", "Rao", "Das", "Bose", "Chopra", "Mehta", "Jain", "Shah",
    "Agarwal", "Pillai", "Menon", "Banerjee", "Chatterjee", "Mukherjee",
    "Desai", "Kapoor", "Malhotra", "Joshi", "Saxena", "Bhat", "Nayak", "Sinha",
)

# (city, state, pincode_prefix) — pincode India = 6 chữ số, prefix theo vùng.
_IN_CITIES = (
    ("Mumbai", "Maharashtra", "4000"),
    ("Delhi", "Delhi", "1100"),
    ("Bengaluru", "Karnataka", "5600"),
    ("Chennai", "Tamil Nadu", "6000"),
    ("Hyderabad", "Telangana", "5000"),
    ("Kolkata", "West Bengal", "7000"),
    ("Pune", "Maharashtra", "4110"),
    ("Ahmedabad", "Gujarat", "3800"),
    ("Jaipur", "Rajasthan", "3020"),
    ("Lucknow", "Uttar Pradesh", "2260"),
)

_IN_STREETS = (
    "MG Road", "Brigade Road", "Linking Road", "Park Street", "Anna Salai",
    "Connaught Place", "Banjara Hills", "Koramangala", "Andheri West",
    "Salt Lake", "Jubilee Hills", "Indiranagar", "Sector 18", "Civil Lines",
)


def random_india_phone() -> str:
    """Số di động Ấn Độ hợp lệ: +91 + 10 chữ số, bắt đầu 6-9."""
    first = secrets.choice("6789")
    rest = "".join(secrets.choice(string.digits) for _ in range(9))
    return f"+91{first}{rest}"


def random_india_profile() -> dict:
    """Profile + billing Ấn Độ đầy đủ để điền form (name, phone, address...).

    Trả về superset của ``random_profile()`` + các field địa chỉ India:
    name, first_name, last_name, age, password, birthdate, phone,
    address_line1, city, state, postal_code, country, country_code.
    """
    first = secrets.choice(_IN_FIRST_NAMES)
    last = secrets.choice(_IN_LAST_NAMES)

    city, state, pin_prefix = secrets.choice(_IN_CITIES)
    house_no = secrets.randbelow(999) + 1
    street = secrets.choice(_IN_STREETS)
    postal_code = f"{pin_prefix}{secrets.randbelow(100):02d}"

    birthdate = _default_birthdate()
    return {
        "name": f"{first} {last}",
        "first_name": first,
        "last_name": last,
        "age": _age_from_birthdate(birthdate),
        "password": random_password(),
        "birthdate": birthdate,
        "phone": random_india_phone(),
        "address_line1": f"{house_no}, {street}",
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "country": "India",
        "country_code": "IN",
    }

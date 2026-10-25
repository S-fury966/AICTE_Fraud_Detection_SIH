"""The demo world: ten synthetic applications, each with a story.

Every institution, person, coordinate and certificate here is invented. The
certificate numbers come from the seeded demo issuer registry
(`data/demo/issuer_registry.json`), so genuine certificates verify against it.

Each scenario says what was injected, and the labels for the demo evaluation
are written from that - never from what a detector returns (the step 7
independence rule).
"""

from __future__ import annotations

from typing import Any

SEED = 20261025
AS_OF = "2026-10-20"
RECORDS_CAPTURED_ON = "2026-09-15"
WEBSITE_CAPTURED_ON = "2026-10-01"

DEPARTMENTS = (
    "Computer Science and Engineering",
    "Electronics and Communication Engineering",
    "Mechanical Engineering",
    "Civil Engineering",
    "Electrical Engineering",
    "Information Technology",
    "Applied Sciences and Humanities",
)

# Spelling the website uses for each department, so the alias table is
# exercised: a website rarely writes the name the way the records do.
WEBSITE_SPELLING = {
    "Computer Science and Engineering": "Department of Computer Science & Engineering",
    "Electronics and Communication Engineering": "Electronics & Communication Engineering",
    "Mechanical Engineering": "Mechanical Engineering",
    "Civil Engineering": "Civil Engineering",
    "Electrical Engineering": "Electrical and Electronics Engineering",
    "Information Technology": "Information Technology",
    "Applied Sciences and Humanities": "Humanities and Sciences",
}

# Declared CSVs use the short codes, as an institution's own spreadsheet would.
DECLARED_SPELLING = {
    "Computer Science and Engineering": "CSE",
    "Electronics and Communication Engineering": "ECE",
    "Mechanical Engineering": "Mechanical",
    "Civil Engineering": "Civil",
    "Electrical Engineering": "EEE",
    "Information Technology": "IT",
    "Applied Sciences and Humanities": "Applied Sciences",
}

INSTITUTIONS: list[dict[str, Any]] = [
    {
        "key": "shantiniketan",
        "name": "Shantiniketan Institute of Technology",
        "city": "Bolpur (demo)",
        "location": {"lat": 23.6693, "lon": 87.6857},
        "departments": {"Computer Science and Engineering": 14, "Electronics and Communication Engineering": 10, "Mechanical Engineering": 9, "Applied Sciences and Humanities": 8},
    },
    {
        "key": "malabar",
        "name": "Malabar Institute of Technology",
        "city": "Kozhikode (demo)",
        "location": {"lat": 11.2588, "lon": 75.7804},
        "departments": {"Computer Science and Engineering": 12, "Civil Engineering": 8, "Electrical Engineering": 9, "Applied Sciences and Humanities": 7},
    },
    {
        "key": "deccan",
        "name": "Deccan Plateau Institute of Engineering",
        "city": "Pune (demo)",
        "location": {"lat": 18.5204, "lon": 73.8567},
        "departments": {"Computer Science and Engineering": 12, "Mechanical Engineering": 10, "Information Technology": 8, "Applied Sciences and Humanities": 6},
    },
    {
        "key": "aravalli",
        "name": "Aravalli Institute of Technology and Research",
        "city": "Udaipur (demo)",
        "location": {"lat": 24.5854, "lon": 73.7125},
        "departments": {"Computer Science and Engineering": 11, "Civil Engineering": 7, "Mechanical Engineering": 8, "Applied Sciences and Humanities": 6},
    },
    {
        "key": "gomati",
        "name": "Gomati Valley College of Engineering",
        "city": "Lucknow (demo)",
        "location": {"lat": 26.8467, "lon": 80.9462},
        "departments": {"Computer Science and Engineering": 10, "Electronics and Communication Engineering": 8, "Civil Engineering": 7, "Applied Sciences and Humanities": 6},
        # 240 students over 10 recorded faculty is 24:1, above the mock 20:1
        # norm; the 14 declared bring it to 17:1. The inflation has a motive.
        "intake_override": {"Computer Science and Engineering": 240},
    },
    {
        "key": "vindhya",
        "name": "Vindhya Institute of Technology",
        "city": "Jabalpur (demo)",
        "location": {"lat": 23.1815, "lon": 79.9864},
        "departments": {"Computer Science and Engineering": 9, "Mechanical Engineering": 8, "Electrical Engineering": 7, "Applied Sciences and Humanities": 5},
    },
    {
        "key": "chenab",
        "name": "Chenab Valley College of Technology",
        "city": "Jammu (demo)",
        "location": {"lat": 32.7266, "lon": 74.8570},
        "departments": {"Computer Science and Engineering": 12, "Civil Engineering": 6, "Electronics and Communication Engineering": 7, "Applied Sciences and Humanities": 5},
    },
    {
        "key": "nilgiri",
        "name": "Nilgiri College of Engineering and Technology",
        "city": "Coimbatore (demo)",
        "location": {"lat": 11.0168, "lon": 76.9558},
        "departments": {"Computer Science and Engineering": 10, "Mechanical Engineering": 9, "Information Technology": 6, "Applied Sciences and Humanities": 6},
    },
    {
        "key": "harita",
        "name": "Harita Institute of Science and Technology",
        "city": "Guntur (demo)",
        "location": {"lat": 16.3067, "lon": 80.4365},
        "departments": {"Computer Science and Engineering": 8, "Civil Engineering": 6, "Electrical Engineering": 6, "Applied Sciences and Humanities": 5},
    },
    {
        "key": "brahmaputra",
        "name": "Brahmaputra College of Engineering",
        "city": "Guwahati (demo)",
        "location": {"lat": 26.1445, "lon": 91.7362},
        "departments": {"Computer Science and Engineering": 9, "Civil Engineering": 7, "Mechanical Engineering": 7, "Applied Sciences and Humanities": 5},
    },
    {
        "key": "saraswati",
        "name": "Saraswati Institute of Technology and Management",
        "city": "Nashik (demo)",
        "location": {"lat": 19.9975, "lon": 73.7898},
        "departments": {"Computer Science and Engineering": 8, "Mechanical Engineering": 6, "Applied Sciences and Humanities": 4},
    },
]

# How each department count is distorted in the DECLARED list.
#   "match"           - declared = AICTE roster
#   {"dept": +n/-n}   - add or remove people in that department
#   "borrow:<key>:<n>" handled separately: replace n own CSE staff with n of
#                      another institution's CSE staff, ids and all.

# A lookalike host differing by one character, under the reserved .test TLD.
LOOKALIKE_HOST = "verify.nbta-dem0.test"
GENUINE_HOST = "verify.nbta-demo.test"

SCENARIOS: list[dict[str, Any]] = [
    {
        "key": "shantiniketan",
        "summary": "Clean application. Genuine certificate, consistent faculty, ordinary lab photos.",
        "certificate": {"record": "NBTA/DEMO/2024/00417"},
        "faculty": {"mode": "match"},
        "website": {},
        "photos": [
            {"name": "computer_lab_1.jpg", "type": "lab_photo", "scene": 101, "kind": "lab", "claim": "Computer Lab 1 - 30 systems", "exif": "camera"},
            {"name": "classroom_a.jpg", "type": "classroom_photo", "scene": 102, "kind": "classroom", "claim": "Classroom A-101", "exif": "camera"},
        ],
        "is_fraud": False,
        "fraud_types": [],
    },
    {
        "key": "malabar",
        "summary": "Clean application whose certificate is later reused by another institution. Website snapshot is stale; one photo has no EXIF.",
        "certificate": {"record": "NBTA/DEMO/2024/00622"},
        "faculty": {"mode": "match"},
        "website": {"captured_on": "2025-12-01"},
        "photos": [
            {"name": "electrical_lab.jpg", "type": "lab_photo", "scene": 103, "kind": "lab", "claim": "Electrical Machines Lab", "exif": "camera"},
            {"name": "seminar_hall.jpg", "type": "classroom_photo", "scene": 104, "kind": "classroom", "claim": "Seminar Hall", "exif": "none"},
        ],
        "is_fraud": False,
        "fraud_types": [],
    },
    {
        "key": "deccan",
        "summary": "Genuine certificate. Faculty moved between departments on paper (total unchanged). One classroom image carries an image-generator marker.",
        "certificate": {"record": "NBTA/DEMO/2024/00621"},
        "faculty": {"mode": "adjust", "adjust": {"Computer Science and Engineering": +4, "Mechanical Engineering": -4}},
        "website": {},
        "photos": [
            {"name": "it_lab.jpg", "type": "lab_photo", "scene": 105, "kind": "lab", "claim": "IT Lab 2 - 24 systems", "exif": "camera"},
            {"name": "smart_classroom.png", "type": "classroom_photo", "scene": 106, "kind": "classroom", "claim": "Smart Classroom B-204", "exif": "generated"},
        ],
        "is_fraud": True,
        "fraud_types": ["inflated_faculty", "ai_generated_image"],
    },
    {
        "key": "aravalli",
        "summary": "Clean, digitally signed certificate. Declared faculty one above records in one department - inside tolerance (a hard negative).",
        "certificate": {"record": "NBTA/DEMO/2024/00635", "sign": "intact"},
        "faculty": {"mode": "adjust", "adjust": {"Computer Science and Engineering": +1}},
        "website": {},
        "photos": [
            {"name": "workshop.jpg", "type": "lab_photo", "scene": 107, "kind": "lab", "claim": "Central Workshop", "exif": "camera"},
        ],
        "is_fraud": False,
        "fraud_types": [],
    },
    {
        "key": "gomati",
        "summary": "Certificate number edited by one digit (QR still carries the original). CSE faculty inflated by 40% over records.",
        "certificate": {"record": "NBTA/DEMO/2024/00418", "print_number": "NBTA/DEMO/2024/00481"},
        "faculty": {"mode": "adjust", "adjust": {"Computer Science and Engineering": +4}},
        "website": {},
        "photos": [
            {"name": "cse_lab.jpg", "type": "lab_photo", "scene": 108, "kind": "lab", "claim": "CSE Lab - 36 systems", "exif": "camera"},
        ],
        "is_fraud": True,
        "fraud_types": ["edited_certificate_number", "inflated_faculty"],
    },
    {
        "key": "vindhya",
        "summary": "Genuine certificate number, but the QR points to a lookalike verification domain. A lab photo is Shantiniketan's, mirrored and shrunk.",
        "certificate": {"record": "NBTA/DEMO/2024/00604", "qr_host": LOOKALIKE_HOST},
        "faculty": {"mode": "match"},
        "website": {},
        "photos": [
            {"name": "computer_centre.jpg", "type": "lab_photo", "scene": 101, "kind": "lab", "claim": "Computer Centre - 40 systems", "exif": "none", "reuse_of": "shantiniketan/computer_lab_1.jpg"},
            {"name": "mech_lab.jpg", "type": "lab_photo", "scene": 109, "kind": "lab", "claim": "Mechanical Lab", "exif": "camera"},
        ],
        "is_fraud": True,
        "fraud_types": ["qr_fake_domain", "reused_lab_photo"],
    },
    {
        "key": "chenab",
        "summary": "Submits Malabar's certificate number under its own name. Four CSE faculty are Deccan's, faculty ids and all.",
        "certificate": {"record": "NBTA/DEMO/2024/00622", "as_institution": True},
        "faculty": {"mode": "borrow", "from": "deccan", "count": 4},
        "website": {},
        "photos": [
            {"name": "ece_lab.jpg", "type": "lab_photo", "scene": 110, "kind": "lab", "claim": "ECE Lab", "exif": "camera"},
        ],
        "is_fraud": True,
        "fraud_types": ["duplicate_certificate", "shared_faculty"],
    },
    {
        "key": "nilgiri",
        "summary": "Certificate genuinely issued but since revoked by the issuer (honest document, needs review). One lab photo has a cloned region and an editor tag.",
        "certificate": {"record": "NBTA/DEMO/2023/00204"},
        "faculty": {"mode": "match"},
        "website": {},
        "photos": [
            {"name": "physics_lab.jpg", "type": "lab_photo", "scene": 111, "kind": "lab", "claim": "Physics Lab - 20 workstations", "exif": "edited", "manipulation": "clone"},
            {"name": "drawing_hall.jpg", "type": "classroom_photo", "scene": 112, "kind": "classroom", "claim": "Drawing Hall", "exif": "camera", "manipulation": "paste"},
        ],
        "is_fraud": True,
        "fraud_types": ["photo_manipulation"],
        "expected_honest_flags": ["issuer_status_not_active"],
    },
    {
        "key": "harita",
        "summary": "Certificate expired in 2019 (honest document, needs review). Lab photo geotagged ~600 km away with a capture date after upload.",
        "certificate": {"record": "NBTA/DEMO/2019/00031"},
        "faculty": {"mode": "match"},
        "website": {},
        "photos": [
            {"name": "civil_lab.jpg", "type": "lab_photo", "scene": 113, "kind": "lab", "claim": "Civil Engineering Lab", "exif": "elsewhere"},
        ],
        "is_fraud": True,
        "fraud_types": ["photo_location"],
        "expected_honest_flags": ["issuer_expired", "accreditation_does_not_cover_session"],
    },
    {
        "key": "brahmaputra",
        "summary": "Digitally signed certificate whose number was edited after signing: the signature no longer matches the bytes.",
        "certificate": {"record": "NBTA/DEMO/2024/00634", "sign": "tampered", "tamper": ["00634", "00684"]},
        "faculty": {"mode": "match"},
        "website": {},
        "photos": [
            {"name": "library_lab.jpg", "type": "lab_photo", "scene": 114, "kind": "lab", "claim": "Digital Library - 18 systems", "exif": "camera"},
        ],
        "is_fraud": True,
        "fraud_types": ["edited_certificate_number"],
    },
    {
        "key": "saraswati",
        "summary": "Submits another institution's genuine certificate (it verifies at the issuer, but names Konkan Coast). The faculty list and lab photos are missing.",
        "certificate": {"record": "NBTA/DEMO/2024/00422"},
        "faculty": None,
        "website": {},
        "photos": [
            {"name": "classroom_c.jpg", "type": "classroom_photo", "scene": 115, "kind": "classroom", "claim": "Classroom C-12", "exif": "camera"},
        ],
        "is_fraud": True,
        "fraud_types": ["certificate_of_other_institution"],
        "expected_honest_flags": ["application_document_missing"],
    },
]

# What each injected fraud type should produce. A group is satisfied when ANY
# of its types fires at or above `min_strength`.
EXPECTED_BY_FRAUD_TYPE: dict[str, dict[str, Any]] = {
    "edited_certificate_number": {"any_of": ["issuer_record_not_found", "qr_certificate_id_mismatch", "signature_digest_mismatch", "issuer_field_mismatch"], "min_strength": "strong"},
    "qr_fake_domain": {"any_of": ["qr_domain_not_allowlisted", "qr_deceptive_url_pattern"], "min_strength": "strong"},
    "duplicate_certificate": {"any_of": ["certificate_number_reuse", "issuer_field_mismatch", "issuer_qr_id_other_institution"], "min_strength": "strong"},
    "inflated_faculty": {"any_of": ["faculty_count_exceeds_records", "faculty_counts_redistributed"], "min_strength": "medium"},
    "shared_faculty": {"any_of": ["faculty_shared_across_institutions"], "min_strength": "medium"},
    "reused_lab_photo": {"any_of": ["photo_reused_across_submissions"], "min_strength": "medium"},
    "ai_generated_image": {"any_of": ["photo_possible_synthetic_content"], "min_strength": "weak"},
    "photo_manipulation": {"any_of": ["photo_copy_move_region", "photo_ela_inconsistency"], "min_strength": "weak"},
    "photo_location": {"any_of": ["photo_gps_far_from_institution", "photo_capture_date_after_upload"], "min_strength": "medium"},
    "certificate_of_other_institution": {"any_of": ["certificate_names_other_institution"], "min_strength": "strong"},
}

FIRST_NAMES = (
    "Aarav", "Ananya", "Arjun", "Bhavna", "Chirag", "Deepa", "Eshan", "Farah", "Gaurav", "Harini",
    "Ishaan", "Jaya", "Kabir", "Lakshmi", "Manav", "Nandini", "Omkar", "Pallavi", "Rohan", "Sanya",
    "Tarun", "Uma", "Varun", "Waheeda", "Yash", "Zoya", "Aditi", "Bharat", "Charu", "Dhruv",
    "Esha", "Firoz", "Gita", "Hemant", "Indira", "Jatin", "Kavya", "Lalit", "Meera", "Naveen",
)
LAST_NAMES = (
    "Acharya", "Bhatt", "Chandran", "Deshmukh", "Edathil", "Fernandes", "Ghosh", "Hegde", "Iyer", "Joshi",
    "Kulkarni", "Lal", "Menon", "Nair", "Oberoi", "Pillai", "Qureshi", "Rao", "Saxena", "Thakur",
    "Upadhyay", "Varma", "Wadia", "Yadav", "Zaveri", "Banerjee", "Chatterjee", "Dutta", "Gill", "Kapoor",
)
DESIGNATIONS = ("Professor", "Associate Professor", "Assistant Professor", "Assistant Professor", "Assistant Professor")
QUALIFICATIONS = ("PhD", "PhD", "M.Tech", "M.Tech", "M.E.", "M.Sc")

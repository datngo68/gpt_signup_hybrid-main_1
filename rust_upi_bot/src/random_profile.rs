//! India billing profile generator — port từ `random_profile.py`.
//!
//! Sinh random first/last name + city/state/pincode + phone +
//! address_line1 — đồng bộ logic với Python để billing payload identical.

use rand::seq::SliceRandom;
use rand::Rng;
use serde::{Deserialize, Serialize};

const IN_FIRST_NAMES: &[&str] = &[
    "Aarav", "Aditya", "Arjun", "Ayaan", "Dhruv", "Ishaan", "Kabir", "Karan",
    "Krishna", "Reyansh", "Rohan", "Rudra", "Sai", "Shaurya", "Vihaan", "Vivaan",
    "Aanya", "Aadhya", "Ananya", "Anika", "Diya", "Ira", "Kavya", "Myra",
    "Navya", "Neha", "Pari", "Pooja", "Priya", "Riya", "Saanvi", "Tara",
];

const IN_LAST_NAMES: &[&str] = &[
    "Sharma", "Verma", "Gupta", "Singh", "Kumar", "Patel", "Reddy", "Nair",
    "Iyer", "Rao", "Das", "Bose", "Chopra", "Mehta", "Jain", "Shah",
    "Agarwal", "Pillai", "Menon", "Banerjee", "Chatterjee", "Mukherjee",
    "Desai", "Kapoor", "Malhotra", "Joshi", "Saxena", "Bhat", "Nayak", "Sinha",
];

/// (city, state, pincode_prefix) — pincode India 6 chữ số, prefix theo vùng.
const IN_CITIES: &[(&str, &str, &str)] = &[
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
];

const IN_STREETS: &[&str] = &[
    "MG Road", "Brigade Road", "Linking Road", "Park Street", "Anna Salai",
    "Connaught Place", "Banjara Hills", "Koramangala", "Andheri West",
    "Salt Lake", "Jubilee Hills", "Indiranagar", "Sector 18", "Civil Lines",
];

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IndiaProfile {
    pub name: String,
    pub address_line1: String,
    pub city: String,
    pub state: String,
    pub postal_code: String,
}

pub fn random_india_profile() -> IndiaProfile {
    let mut rng = rand::thread_rng();
    let first = IN_FIRST_NAMES.choose(&mut rng).copied().unwrap_or("Aarav");
    let last = IN_LAST_NAMES.choose(&mut rng).copied().unwrap_or("Sharma");
    let (city, state, pin_prefix) = IN_CITIES.choose(&mut rng).copied().unwrap_or(IN_CITIES[0]);
    let house_no: u32 = rng.gen_range(1..=999);
    let street = IN_STREETS.choose(&mut rng).copied().unwrap_or("MG Road");
    let postal_suffix: u32 = rng.gen_range(0..100);
    let postal_code = format!("{}{:02}", pin_prefix, postal_suffix);

    IndiaProfile {
        name: format!("{} {}", first, last),
        address_line1: format!("{}, {}", house_no, street),
        city: city.to_string(),
        state: state.to_string(),
        postal_code,
    }
}

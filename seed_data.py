"""
Run once to populate the initial knowledge base:
    python seed_data.py
"""
from app.database import SessionLocal, engine, Base
from app import models

Base.metadata.create_all(bind=engine)
db = SessionLocal()

entries = [
    dict(
        question_canonical="What right do cyclists have to be on the road?",
        question_variants=[
            "why do cyclists get to use the road",
            "cyclists don't pay taxes for roads",
            "roads are for cars not bikes",
        ],
        answer=(
            "Under Texas law, bicycles are legally classified as vehicles, and cyclists have "
            "the same rights and duties as other vehicle operators (Texas Transportation Code "
            "§551.101). On the tax point: road funding comes mostly from general funds, "
            "property taxes, and sales taxes — not just the gas tax — and cyclists pay those "
            "like everyone else. Plus, most cyclists also own cars. Roads aren't \"for cars\"; "
            "they're shared public infrastructure, same as sidewalks are for pedestrians even "
            "though pedestrians don't pay a special sidewalk tax."
        ),
        category="legal",
        sources=[{"url": "https://www.bikehouston.org/laws",
                   "note": "BikeHouston's own Texas/Houston bike law compilation"}],
        volatile=True,
        recheck_interval_days=365,
    ),
    dict(
        question_canonical="How come cyclists don't have to carry insurance?",
        question_variants=[
            "why don't bikers need insurance",
            "cyclists could hit someone and have no insurance",
        ],
        answer=(
            "Texas requires liability insurance for motor vehicles because they're registered "
            "and licensed; bicycles aren't motorized or registered, so they fall outside that "
            "system. That said, cyclists can be held liable in a crash just like anyone else — "
            "Texas's vehicle classification brings the same legal exposure a driver has, "
            "including being sued for damages. Many cyclists carry coverage voluntarily through "
            "homeowner's/renter's policies or club memberships."
        ),
        category="legal",
        sources=[{"url": "https://www.bikehouston.org/laws",
                   "note": "BikeHouston bike law compilation (§551.101 classification)"}],
        volatile=True,
        recheck_interval_days=365,
    ),
    dict(
        question_canonical=(
            "How come cyclists have the right to ride on the road and e-motos "
            "traveling at the same speed do not have this right?"
        ),
        question_variants=[
            "why can't e-motos ride where bikes ride",
            "sur-ron talaria road legal texas",
        ],
        answer=(
            "This comes down to legal classification, not speed. Texas law (§551.106) actually "
            "protects legal e-bikes' road and path access the same as regular bicycles — a city "
            "can't ban a compliant e-bike from roads where regular bikes are allowed. But "
            "high-power \"e-motos\" (Sur-Ron, Talaria, and similar) exceed the 750-watt/28-mph "
            "cap that defines a legal e-bike, so they don't qualify — they're treated as "
            "unregistered motor vehicles, which generally can't legally be ridden on public "
            "roads without registration, a license, and insurance. It's about what category the "
            "vehicle falls into, not what speed it's capable of."
        ),
        category="legal",
        sources=[
            {"url": "https://www.bikehouston.org/laws",
             "note": "BikeHouston: Class 1/2/3 e-bikes permitted anywhere non-electric bikes are, §551.107"},
            {"url": "https://ebikeoracle.com/laws/texas",
             "note": "Texas e-bike vs e-moto classification detail, §551.106 and §664.001"},
        ],
        volatile=True,
        recheck_interval_days=180,
    ),
    dict(
        question_canonical="Why can't cyclists stick to bike trails?",
        question_variants=[
            "bike trails were built to keep cyclists off the road",
            "just use the trail instead of the street",
        ],
        answer=(
            "Texas law doesn't require cyclists to use an off-road path even when one exists "
            "next to the road — riding on the roadway is a legal right, not a fallback. Trails "
            "also don't go everywhere: they don't connect to most homes, workplaces, or grocery "
            "stores, and many streets have no adjacent trail at all. Bike lanes and trails "
            "supplement road access for safety and comfort — they were never meant to replace "
            "the legal right to ride on the street."
        ),
        category="legal",
        sources=[
            {"url": "https://www.bikehouston.org/laws",
             "note": "BikeHouston: use of adjacent bike paths is optional, not required, §551.103"},
            {"url": "https://www.txdot.gov/safety/bicycle-pedestrian-safety/laws-regulations-faq.html",
             "note": "TxDOT bicycle law FAQ"},
        ],
        volatile=False,
        recheck_interval_days=None,
    ),
]

for e in entries:
    db.add(models.FAQEntry(**e))

db.commit()
print(f"Seeded {len(entries)} FAQ entries.")
db.close()

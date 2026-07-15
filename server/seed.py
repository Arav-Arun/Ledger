import os
import sys
from openai import OpenAI
from dotenv import load_dotenv

# Ensure we can import from server directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import store
import memory

load_dotenv()

DEFAULT_CUSTOMERS = [
    {
        "id": "priya_sharma",
        "name": "Priya Sharma",
        "memories": [
            {"text": "Customer has a pending refund on a returned coffee grinder (return RET-4821, glass cracked on arrival).", "category": "issue"},
            {"text": "Customer was promised the coffee-grinder refund would be processed within 5-7 working days.", "category": "commitment"},
            {"text": "Customer prefers short, jargon-free replies.", "category": "preference"},
            {"text": "Customer prefers deliveries left with building security, never at the door.", "category": "preference"},
            {"text": "Customer is vegetarian and avoids purchasing leather items.", "category": "profile"}
        ]
    },
    {
        "id": "rahul_verma",
        "name": "Rahul Verma",
        "memories": [
            {"text": "Customer is travelling abroad and requested to pause all deliveries.", "category": "episode"},
            {"text": "Customer was promised a 10% goodwill discount code on their next order.", "category": "commitment"},
            {"text": "Customer prefers to receive updates via WhatsApp.", "category": "preference"},
            {"text": "Customer mainly purchases camera and photography equipment.", "category": "profile"}
        ]
    },
    {
        "id": "vikram_nair",
        "name": "Vikram Nair",
        "memories": [
            {"text": "Customer reported a desk lamp (order ORD-2290) marked delivered but never received; escalation is open.", "category": "issue"},
            {"text": "Customer was promised a free replacement desk lamp.", "category": "commitment"},
            {"text": "Customer works late-night shifts and requested delivery drivers call only before 11:00 AM.", "category": "preference"},
            {"text": "Customer lives in a gated community requiring couriers to check in at the front gate.", "category": "profile"}
        ]
    },
    {
        "id": "fatima_sheikh",
        "name": "Fatima Sheikh",
        "memories": [
            {"text": "Customer is waiting on a refund for returned running shoes (order ORD-5512, wrong size).", "category": "issue"},
            {"text": "Customer was issued a 500 INR store credit as an apology for shipping delays.", "category": "commitment"},
            {"text": "Customer prefers receiving updates via WhatsApp.", "category": "preference"},
            {"text": "Customer primarily purchases kids' clothing and toys as gifts.", "category": "profile"}
        ]
    },
    {
        "id": "daniel_thomas",
        "name": "Daniel Thomas",
        "memories": [
            {"text": "Customer prefers orders to be gift-wrapped with a handwritten gift note.", "category": "preference"},
            {"text": "Customer prefers shipping to office on weekdays and home address on weekends.", "category": "profile"},
            {"text": "Customer is sensitive to strong chemical fragrances.", "category": "profile"},
            {"text": "Customer prefers email communications and dislikes phone calls.", "category": "preference"}
        ]
    },
    {
        "id": "ananya_iyer",
        "name": "Ananya Iyer",
        "memories": [
            {"text": "Customer recently signed up and prefers SMS updates.", "category": "preference"},
            {"text": "Customer is highly interested in fitness, home workout gear, and yoga.", "category": "profile"}
        ]
    }
]

def get_embedding(text: str) -> list[float]:
    client = OpenAI()
    res = client.embeddings.create(
        model="text-embedding-3-small",
        input=[text]
    )
    return res.data[0].embedding

def seed_db():
    print("Initializing database connection pool...")
    store.init()
    
    print("Clearing existing tables...")
    with store.pool().connection() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM memory_events")
            conn.execute("DELETE FROM memories")
            conn.execute("DELETE FROM customers")
            
    print("Seeding default customers and memories...")
    for cust in DEFAULT_CUSTOMERS:
        print(f"Creating customer: {cust['name']} ({cust['id']})...")
        store.create_customer(cust["id"], cust["name"])
        
        for mem in cust["memories"]:
            print(f"  Adding memory: '{mem['text']}'...")
            emb = get_embedding(mem["text"])
            store.insert_memory(
                customer_id=cust["id"],
                text=mem["text"],
                category=mem["category"],
                embedding=emb,
                source="initial seeding"
            )
            
    print("\nDatabase seeding completed successfully!")
    store.close()

if __name__ == "__main__":
    if not os.getenv("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    if not os.getenv("DATABASE_URL"):
        print("Error: DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)
        
    try:
        seed_db()
    except Exception as e:
        print(f"Seeding failed: {e}", file=sys.stderr)
        sys.exit(1)

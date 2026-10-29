"""What code declares: routes, operations, tables and jobs, found by syntax only.

One small file per framework; every finding names its file and line. Attaching them to
a topology draws each under the app that holds it, within a budget.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "ontology-extraction/scripts"
sys.path.insert(0, str(SCRIPTS))
import code_surface
import discover_repo

FILES = {
    # Next.js app router: route handlers and pages; groups and slots add no segment.
    "web/app/api/(billing)/checkout/route.ts": "export async function POST(req) {}\nexport const GET = () => {};\n",
    "web/app/(marketing)/pricing/page.tsx": "export default function Pricing() {}\n",
    "web/pages/api/hello.ts": "export default function handler(req, res) {}\n",
    "web/pages/blog/[slug].tsx": "export default function Post() {}\n",
    "web/pages/_app.tsx": "export default function App() {}\n",
    # Remix / React Router file routes.
    "remix/app/routes/api+/stripe.webhook.ts": "export async function action({ request }) {}\n",
    "remix/app/routes/_auth.signin.$id.tsx": "export const loader = () => null;\nexport default function S() {}\n",
    "api/server.js": "const app = express();\napp.get('/health', ok);\nrouter.post(\"/orders/:id/refund\", refund);\n",
    "py/app.py": "@app.route('/items', methods=['GET', 'POST'])\ndef items():\n    pass\n\n@router.delete(\"/items/{id}\")\n"
                 "async def remove(id):\n    pass\n",
    "dj/shop/urls.py": "urlpatterns = [\n    path('checkout/', views.checkout),\n    re_path(r'^api/', include(api)),\n]\n",
    "dj/shop/models.py": "from django.db import models\n\nclass Order(models.Model):\n    class Meta:\n        pass\n\n"
                         "class Helper:\n    pass\n",
    # SQLModel: only table=True classes are tables; pydantic schemas in a models.py are not.
    "fa/app/models.py": "from sqlmodel import SQLModel\n\nclass UserBase(SQLModel):\n    email: str\n\n"
                        "class User(UserBase, table=True):\n    id: int\n\nclass Token(BaseModel):\n    value: str\n",
    # A router prefix applies to the file's routes; the line is the decorator's, not the blank line above it.
    "fa/app/routes/items.py": "router = APIRouter(prefix=\"/items\", tags=[\"items\"])\n\n\n"
                              "@router.get(\"/{id}\")\ndef read(id):\n    pass\n",
    "go/main.go": "r.Get(\"/users\", list)\nmux.HandleFunc(\"POST /users/{id}\", update)\ne.DELETE(\"/users/:id\", del)\n",
    "java/OrderController.java": "@GetMapping(\"/orders\")\npublic List<Order> all() {}\n",
    "java/Order.java": "@Entity\n@Table(name = \"orders\")\npublic class Order {}\n",
    # A class-level @RequestMapping prefixes the methods; a mention in a comment is not a route.
    "java/PetController.java": "/**\n * Runs before each @RequestMapping method.\n */\n@Controller\n@RequestMapping(\"/owners/{ownerId}\")\n"
                               "class PetController {\n  @GetMapping(\"/pets/new\")\n  String form() {}\n"
                               "  @GetMapping({ \"/vets\", \"/vets.html\" })\n  String vets() {}\n"
                               "  @Scheduled(cron = \"0 0 * * * *\")\n  public void evictCaches() {}\n}\n",
    "java/Owner.java": "@Entity\npublic class Owner extends Person {}\n",
    "rb/config/routes.rb": "Rails.application.routes.draw do\n  resources :invoices\n  post '/pay', to: 'pay#create'\n"
                           "  namespace :api do\n    namespace :v1 do\n      resources :categories do\n"
                           "        resources :deletions, only: :create\n        post :bootstrap, on: :collection\n"
                           "        member do\n          get :export\n        end\n      end\n    end\n  end\n"
                           "  mount Sidekiq::Web => \"/sidekiq\"\nend\n",
    "rb/app/jobs/sync_job.rb": "class ApplicationJob < ActiveJob::Base\nend\nclass SyncJob < ApplicationJob\nend\n",
    "rb/app/workers/hard_worker.rb": "class HardWorker\n  include Sidekiq::Job\nend\n",
    "rb/config/schedule.yml": "clean_syncs:\n  cron: \"0 * * * *\" # hourly\n  class: \"SyncCleanerJob\"\n\n"
                              "not_a_job:\n  queue: default\n",
    "rb/db/schema.rb": "ActiveRecord::Schema.define do\n  create_table \"invoices\" do |t|\n  end\nend\n",
    "php/routes/web.php": "<?php\nRoute::get('/plans', [PlanController::class, 'index']);\n"
                          "Route::middleware(['auth'])->group(function () {\n"
                          "    Route::prefix('vaults')->group(function () {\n"
                          "        Route::get('{vault}', [VaultController::class, 'show']);\n"
                          "        Route::post(\n            '{vault}/contacts',\n            [ContactController::class, 'store']\n        );\n"
                          "    });\n});\nRoute::get('after', [X::class, 'y']);\n",
    "php/routes/api.php": "<?php\nRoute::apiResource('vaults', VaultController::class);\n",
    "php/app/Providers/FortifyServiceProvider.php": "<?php\n$name = $request->get('login.id');\n",
    "php/database/migrations/2024_01_01_create_vaults_table.php": "<?php\nSchema::create('vaults', function (Blueprint $table) {});\n",
    "php/app/Jobs/PushVCard.php": "<?php\nclass PushVCard implements ShouldQueue\n{\n}\n",
    "php/routes/console.php": "<?php\nSchedule::command('model:prune', 'daily');\nSchedule::job(UpdateAddressBooks::class, 'hourly');\n",
    "gql/schema.graphql": "type Query {\n  order(id: ID!): Order\n  orders(\n    first: Int\n  ): [Order]\n}\n"
                          "type Mutation {\n  checkoutComplete(id: ID!): Order\n}\n",
    "trpc/router.ts": "export const appRouter = router({\n  getPlan: publicProcedure.query(() => 1),\n});\n",
    "db/schema.prisma": "model Customer {\n  id Int @id\n}\n",
    "db/migrations/001_init.sql": "CREATE TABLE IF NOT EXISTS \"customer\" (id int);\nCREATE TABLE legacy_audit (id int);\n",
    "db/drizzle.ts": "export const plans = pgTable('plans', {});\n",
    "db/typeorm.ts": "@Entity('subscriptions')\nexport class Subscription {}\n",
    "db/mongoose.js": "module.exports = model('Coupon', schema);\n",
    "db/sequelize.js": "sequelize.define('payouts', {});\n",
    "db/alchemy.py": "class Refund(Base):\n    __tablename__ = 'refunds'\n",
    "jobs/tasks.py": "@shared_task\ndef send_receipt():\n    pass\n\n@app.task(bind=True)\ndef retry_charge(self):\n    pass\n",
    "jobs/settings.py": "CELERY_BEAT_SCHEDULE = {\n    'expire-carts': {\n        'task': 'shop.tasks.expire',\n    },\n}\n",
    "jobs/queue.ts": "const q = new Queue('emails');\ncron.schedule('0 * * * *', tick);\n",
    "node_modules/x/index.js": "app.get('/ignored', f);\n",
    "api/server.test.js": "app.get('/also-read', f);\n",  # tests are read; the overview filters them
}


def make(root: Path) -> Path:
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


class Scan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = make(Path(cls.tmp.name))
        cls.result = code_surface.scan(cls.repo)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def routes(self, framework):
        return sorted((r["method"], r["path"]) for r in self.result["routes"] if r["framework"] == framework)

    def test_routes_by_framework(self):
        self.assertEqual(self.routes("next"), [("ANY", "/api/hello"), ("GET", "/api/checkout"), ("PAGE", "/blog/[slug]"),
                                               ("PAGE", "/pricing"), ("POST", "/api/checkout")])
        self.assertEqual(self.routes("remix"), [("GET", "/signin/:id"), ("POST", "/api/stripe/webhook")])
        self.assertEqual(self.routes("node"), [("GET", "/also-read"), ("GET", "/health"),
                                               ("POST", "/orders/:id/refund")])
        self.assertEqual(self.routes("python"), [("DELETE", "/items/{id}"), ("GET", "/items"), ("GET", "/items/{id}"),
                                                 ("POST", "/items")])
        prefixed = next(r for r in self.result["routes"] if r["file"] == "fa/app/routes/items.py")
        self.assertEqual((prefixed["path"], prefixed["line"]), ("/items/{id}", 4))
        self.assertEqual(self.routes("django"), [("ANY", "/api/"), ("ANY", "/checkout/")])
        self.assertEqual(self.routes("go"), [("DELETE", "/users/:id"), ("GET", "/users"), ("POST", "/users/{id}")])
        self.assertEqual(self.routes("spring"), [("GET", "/orders"), ("GET", "/owners/{ownerId}/pets/new"),
                                                 ("GET", "/owners/{ownerId}/vets"), ("GET", "/owners/{ownerId}/vets.html")])
        self.assertEqual(self.routes("rails"), [
            ("ANY", "/api/v1/categories"), ("ANY", "/api/v1/categories/:category_id/deletions"), ("ANY", "/invoices"),
            ("ANY", "/sidekiq"), ("GET", "/api/v1/categories/:id/export"), ("POST", "/api/v1/categories/bootstrap"),
            ("POST", "/pay")])
        self.assertEqual(self.routes("laravel"), [("ANY", "/api/vaults"), ("GET", "/after"), ("GET", "/plans"),
                                                  ("GET", "/vaults/{vault}"), ("POST", "/vaults/{vault}/contacts")])
        refund = next(r for r in self.result["routes"] if r["path"] == "/orders/:id/refund")
        self.assertEqual((refund["file"], refund["line"]), ("api/server.js", 3))

    def test_operations(self):
        ops = sorted((o["kind"], o["name"]) for o in self.result["operations"])
        self.assertEqual(ops, [("graphql mutation", "checkoutComplete"), ("graphql query", "order"),
                               ("graphql query", "orders"), ("rpc", "getPlan")])  # `first` is an argument

    def test_tables_one_per_name_schema_first(self):
        tables = {t["name"]: (t["source"], t.get("note")) for t in self.result["tables"]}
        self.assertEqual(tables["Customer"], ("prisma", None))
        self.assertNotIn("customer", tables)  # the migration repeats the Prisma model
        self.assertEqual(tables["legacy_audit"], ("sql", "only in migrations"))
        self.assertEqual(tables["User"], ("sqlmodel", None))
        self.assertFalse({"UserBase", "Token"} & set(tables))
        for name, source in (("plans", "drizzle"), ("subscriptions", "typeorm"), ("Coupon", "mongoose"),
                             ("payouts", "sequelize"), ("refunds", "sqlalchemy"), ("Order", "django"),
                             ("invoices", "rails"), ("orders", "jpa"), ("vaults", "laravel"), ("Owner", "jpa")):
            self.assertEqual(tables[name][0], source, name)
        self.assertNotIn("Meta", tables)
        self.assertNotIn("Helper", tables)

    def test_jobs(self):
        jobs = sorted((j["kind"], j["name"]) for j in self.result["jobs"])
        self.assertEqual(jobs, [("bullmq queue", "emails"), ("celery beat schedule", "expire-carts -> shop.tasks.expire"),
                                ("celery task", "retry_charge"), ("celery task", "send_receipt"),
                                ("cron schedule", "0 * * * *"), ("cron schedule", "clean_syncs -> SyncCleanerJob"),
                                ("laravel job", "PushVCard"), ("laravel schedule", "UpdateAddressBooks"),
                                ("laravel schedule", "model:prune"), ("rails job", "HardWorker"), ("rails job", "SyncJob"),
                                ("spring scheduled", "evictCaches")])

    def test_skips_dependencies_and_bounds_output(self):
        self.assertFalse(any("node_modules" in r["file"] for r in self.result["routes"]))
        self.assertEqual(self.result["semantics"], "SYNTAX_CANDIDATES_NOT_VERIFIED_ENDPOINTS_OR_TABLES")
        self.assertEqual(self.result["omitted"], {})


class Attach(unittest.TestCase):
    def test_nodes_go_under_the_app_that_holds_them_within_a_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "compose.yml").write_text("services:\n  api:\n    build: ./api\n")
            (repo / "api").mkdir()
            (repo / "api/package.json").write_text('{"name": "api"}')
            (repo / "api/server.js").write_text("".join(f"app.get('/r{i}', f);\n" for i in range(5)))
            (repo / "web/app/about").mkdir(parents=True)
            (repo / "web/app/about/page.tsx").write_text("export default function A() {}\n")
            (repo / "schema.sql").write_text("CREATE TABLE orders (id int);\n")
            topology = discover_repo.discover(repo)
            api = discover_repo.canonical_id("service", "compose.yml", "api")
            old_budget = code_surface.MAX_MAP_PER_KIND
            code_surface.MAX_MAP_PER_KIND = 4
            try:
                attached = code_surface.attach(topology, repo)
            finally:
                code_surface.MAX_MAP_PER_KIND = old_budget
            routes = [n for n in attached["nodes"] if n["kind"] == "api_route"]
            self.assertEqual([n["label"] for n in routes], ["GET /r0", "GET /r1", "GET /r2", "GET /r3"])  # API before pages
            declares = {e["target"]: e["source"] for e in attached["edges"] if e["relation"] == "DECLARES"}
            self.assertEqual({declares[n["id"]] for n in routes}, {api})
            table = next(n for n in attached["nodes"] if n["kind"] == "declared_db_table")
            self.assertEqual((table["label"], declares[table["id"]]), ("orders", topology["project"]))
            evidence = {e["id"]: e for e in attached["source_evidence"]}
            self.assertEqual(evidence[table["evidence_id"]]["source_kind"], "CODE_SYNTAX_EXTRACTED")
            self.assertIn({"source": "code", "reason": "2 more api_route candidates not drawn (map budget)"},
                          attached["gaps"])
            self.assertEqual(attached["coverage"]["api_route_paths_found"], 6)
            self.assertEqual(len(topology["nodes"]) + 5, len(attached["nodes"]))  # the input is not changed

    def test_the_control_room_draws_code_for_a_local_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make(Path(tmp))
            run = subprocess.run([sys.executable, str(SCRIPTS / "control_room.py"), "--repo", str(repo), "--state-only"],
                                 capture_output=True, text=True, check=True)
            kinds = [n["kind"] for n in json.loads(run.stdout)["nodes"]]
            self.assertGreater(kinds.count("api_route"), 20)
            self.assertEqual(kinds.count("declared_db_table"), 13)


if __name__ == "__main__":
    unittest.main()

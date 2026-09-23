"""Example plugin tool."""
from pydantic import BaseModel, Field
def register(registry, spec):
    class P(BaseModel):
        name: str = Field("عالم", description="اسم للترحيب")
    @registry.tool(spec.name, spec.description, P, tags=spec.tags)
    def hello_tool(params: P, ctx):
        import datetime
        return {"hello": f"مرحبا {params.name} — {datetime.datetime.now().isoformat()} من Plugin!", "plugin": spec.name}

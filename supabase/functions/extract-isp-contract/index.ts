// Extracts Due Diligence terms from an uploaded ISP contract PDF with Claude
// and saves them on the isp_contracts row. Called by the tracker with the
// signed-in user's token, so row-level security still applies to every read
// and write. Needs the ANTHROPIC_API_KEY secret.
import { createClient } from "jsr:@supabase/supabase-js@2";
import { encodeBase64 } from "jsr:@std/encoding@1/base64";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json" } });

const MODEL = Deno.env.get("ISP_EXTRACT_MODEL") || "claude-sonnet-5-5";

const str = { type: ["string", "null"] };
const num = { type: ["number", "null"] };
const bool = { type: ["boolean", "null"] };
const date = { type: ["string", "null"], description: "YYYY-MM-DD, or null if not stated" };

const TOOL = {
  name: "record_isp_contract",
  description: "Record the due diligence terms of one ISP / telecom services agreement for a multifamily property.",
  input_schema: {
    type: "object",
    properties: {
      provider: { ...str, description: "Service provider company (the ISP / cable / telecom operator)." },
      agreement_title: { ...str, description: "Title of the agreement as written." },
      agreement_type: { ...str, description: "One of: Bulk Services, Right of Entry / Access, Marketing, Exclusive Services, Amendment, Letter Agreement, Other." },
      contract_paper: { ...str, description: "Whose form the contract is on: 'ISP' (provider's paper), 'Legal' (owner's counsel / owner paper), 'Consultant' (a consultant or advisor's template), or 'Unknown'." },
      contract_paper_detail: { ...str, description: "What indicates whose paper it is (footer, form number, law firm, consultant name)." },
      property_name_in_contract: str,
      property_address_in_contract: str,
      owner_entity: { ...str, description: "Owner / landlord legal entity." },
      executed_date: { ...date, description: "Date the agreement was fully executed (last signature date if several). YYYY-MM-DD." },
      effective_date: date,
      term_months: { ...num, description: "Initial term in months." },
      term_text: { ...str, description: "Initial term as written, including any trigger (e.g. '10 years from Service Commencement')." },
      expiration_date: { ...date, description: "Expiration date if stated or computable from effective date + term; null if it depends on an unknown event." },
      renewal_terms: { ...str, description: "Automatic renewals, notice periods." },
      escalator: { ...str, description: "Rate escalator as written (e.g. '3% annually on each anniversary', 'CPI capped at 5%')." },
      escalator_pct: { ...num, description: "Annual escalator percentage as a number, e.g. 3 for 3%." },
      rev_share: { ...str, description: "Revenue share on retail services: percentages, tiers, which services, payment timing." },
      door_fee: { ...str, description: "Door / access / signing fees: amount per unit or lump sum, and when paid." },
      door_fee_amount: { ...num, description: "Total door fee in dollars if computable, else per-unit amount." },
      bulk_rate: { ...str, description: "Bulk service rate charged to the owner (per unit per month) and what it includes." },
      units: { type: ["integer", "null"], description: "Number of units covered." },
      anti_bulk: { ...bool, description: "True if the agreement restricts the owner from entering a bulk agreement / prohibits bulk billing (anti-bulk clause)." },
      anti_bulk_detail: str,
      exclusive: { ...bool, description: "True if the provider has exclusive rights (service, marketing, wiring or access exclusivity)." },
      exclusivity_detail: { ...str, description: "What kind of exclusivity and its limits." },
      termination_terms: { ...str, description: "Termination rights, early termination fees, cure periods, rights on sale." },
      services: { type: "array", items: { type: "string" }, description: "Services covered, e.g. Internet, Video, Voice, WiFi, Managed WiFi." },
      line_items: {
        type: "array",
        description: "Every material term as a line item, in contract order: fees, rates, term, renewal, escalator, revenue share, exclusivity, anti-bulk, termination, assignment, wiring/equipment ownership, insurance, service levels, marketing, notices.",
        items: {
          type: "object",
          properties: {
            category: { type: "string", description: "e.g. Term, Fees, Revenue Share, Escalator, Exclusivity, Anti-Bulk, Termination, Assignment, Equipment / Wiring, Service Levels, Marketing, Other" },
            item: { type: "string", description: "Short name of the term." },
            value: { type: "string", description: "The term's value, quoted or closely paraphrased." },
            section: { type: ["string", "null"], description: "Section number and/or page." },
          },
          required: ["category", "item", "value"],
        },
      },
      key_points: { type: "array", items: { type: "string" }, description: "3-8 points an owner's due diligence reviewer should know." },
      red_flags: { type: "array", items: { type: "string" }, description: "Terms that restrict a future bulk program or are unfavorable to the owner." },
    },
    required: ["provider", "line_items"],
  },
};

const PROMPT = `You are reviewing an ISP / telecom services agreement for a multifamily property as part of due diligence for a bulk internet program.
Read the whole document, including amendments, exhibits and signature pages, and record its terms with the record_isp_contract tool.
- Use null when a term is not in the document; never guess. Dates must be YYYY-MM-DD.
- "Executed date" is when the agreement was fully signed; if signature blocks show different dates, use the latest.
- Anti-bulk means a clause that stops the owner from contracting for bulk services or from billing residents for bulk service.
- Exclusive means the provider has any exclusive right (service, marketing, wiring/access).
- List every material term as a line item, with its section or page.`;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "POST only" }, 405);
  const apiKey = Deno.env.get("ANTHROPIC_API_KEY");
  const auth = req.headers.get("Authorization") || "";
  const supabase = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_ANON_KEY")!, {
    global: { headers: { Authorization: auth } },
  });
  let id: number | null = null;
  const fail = async (message: string, status = 500) => {
    if (id != null) await supabase.from("isp_contracts").update({ status: "error", error: message }).eq("id", id);
    return json({ error: message }, status);
  };
  try {
    const body = await req.json();
    id = Number(body.id);
    if (!id) return json({ error: "Missing contract id" }, 400);
    if (!apiKey) return await fail("The ANTHROPIC_API_KEY secret isn't set on the Supabase project, so contracts can't be read yet.");

    const { data: row, error: rowErr } = await supabase.from("isp_contracts").select("id, file_path, file_name").eq("id", id).single();
    if (rowErr || !row) return json({ error: `Contract not found or not allowed: ${rowErr?.message || id}` }, 404);
    if (!row.file_path) return await fail("No file is attached to this contract.");
    await supabase.from("isp_contracts").update({ status: "extracting", error: null }).eq("id", id);

    const { data: file, error: dlErr } = await supabase.storage.from("contract-documents").download(row.file_path);
    if (dlErr || !file) return await fail(`Couldn't read the PDF: ${dlErr?.message || "no file"}`);
    const bytes = new Uint8Array(await file.arrayBuffer());
    if (bytes.length > 30 * 1024 * 1024) return await fail("The PDF is larger than 30 MB; split it and upload the parts.");

    const res = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: { "x-api-key": apiKey, "anthropic-version": "2023-06-01", "content-type": "application/json" },
      body: JSON.stringify({
        model: MODEL,
        max_tokens: 8000,
        tools: [TOOL],
        tool_choice: { type: "tool", name: TOOL.name },
        messages: [{
          role: "user",
          content: [
            { type: "document", source: { type: "base64", media_type: "application/pdf", data: encodeBase64(bytes) }, title: row.file_name || "ISP contract" },
            { type: "text", text: PROMPT },
          ],
        }],
      }),
    });
    const out = await res.json();
    if (!res.ok) return await fail(`Claude couldn't read the contract (${res.status}): ${out?.error?.message || JSON.stringify(out).slice(0, 300)}`);
    const tool = (out.content || []).find((c: { type: string }) => c.type === "tool_use");
    if (!tool) return await fail("Claude didn't return contract terms.");
    const x = tool.input || {};

    const okDate = (v: unknown) => (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v) ? v : null);
    const okNum = (v: unknown) => (typeof v === "number" && isFinite(v) ? v : null);
    const okBool = (v: unknown) => (typeof v === "boolean" ? v : null);
    const okStr = (v: unknown) => (typeof v === "string" && v.trim() ? v.trim() : null);
    const okArr = (v: unknown) => (Array.isArray(v) ? v.filter((s) => typeof s === "string" && s.trim()) : null);
    const update = {
      status: "done", error: null, model: MODEL, extracted_at: new Date().toISOString(), extraction: x,
      provider: okStr(x.provider), agreement_title: okStr(x.agreement_title), agreement_type: okStr(x.agreement_type),
      contract_paper: okStr(x.contract_paper), contract_paper_detail: okStr(x.contract_paper_detail),
      property_name_in_contract: okStr(x.property_name_in_contract), property_address_in_contract: okStr(x.property_address_in_contract),
      owner_entity: okStr(x.owner_entity), executed_date: okDate(x.executed_date), effective_date: okDate(x.effective_date),
      term_months: okNum(x.term_months), term_text: okStr(x.term_text), expiration_date: okDate(x.expiration_date),
      renewal_terms: okStr(x.renewal_terms), escalator: okStr(x.escalator), escalator_pct: okNum(x.escalator_pct),
      rev_share: okStr(x.rev_share), door_fee: okStr(x.door_fee), door_fee_amount: okNum(x.door_fee_amount),
      bulk_rate: okStr(x.bulk_rate), units: Number.isInteger(x.units) ? x.units : null,
      anti_bulk: okBool(x.anti_bulk), anti_bulk_detail: okStr(x.anti_bulk_detail),
      exclusive: okBool(x.exclusive), exclusivity_detail: okStr(x.exclusivity_detail),
      termination_terms: okStr(x.termination_terms), services: okArr(x.services),
      line_items: Array.isArray(x.line_items) ? x.line_items : [], key_points: okArr(x.key_points), red_flags: okArr(x.red_flags),
    };
    const { data: saved, error: upErr } = await supabase.from("isp_contracts").update(update).eq("id", id).select().single();
    if (upErr) return await fail(`Extracted, but saving failed: ${upErr.message}`);
    return json({ contract: saved });
  } catch (e) {
    return await fail(e instanceof Error ? e.message : String(e));
  }
});

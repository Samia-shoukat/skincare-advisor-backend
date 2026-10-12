-- Table for scripts/scrape_products.py.
--
-- Run once in the Supabase dashboard: SQL Editor -> New query -> paste -> Run.
--
-- Separate from `products` on purpose: that table is the backend's curated
-- catalogue, the routine engine recommends from it, and its columns are
-- different. Scraped rows have not been reviewed and must not end up there.

create table if not exists public.scraped_products (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  brand       text not null,
  price       text,
  image_url   text,
  skin_type   text,
  ingredients text,
  -- Lets the script upsert: re-running updates prices and images instead of
  -- inserting the same product twice.
  constraint scraped_products_brand_name_key unique (brand, name)
);

-- Row Level Security on, with read-only public access. The app (anon key) can
-- list products but never write them; the script writes with the service-role
-- key, which bypasses RLS and must stay on your computer or server -- never in
-- the mobile app.
alter table public.scraped_products enable row level security;

drop policy if exists "Anyone can read scraped products" on public.scraped_products;
create policy "Anyone can read scraped products"
  on public.scraped_products
  for select
  using (true);

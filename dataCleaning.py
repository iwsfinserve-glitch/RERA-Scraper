import pandas as pd
import numpy as np

# 1. Load the original CSV
df = pd.read_excel('Goa_RERA_Master.xlsx')

# 2. Define the source name you want to apply to all rows
source_name = 'Goa RERA Master'

# List to hold the unpivoted sub-dataframes
dfs_to_concat = []

# --- Extracting Authorized Persons ---
df_ap = df[['authorized_person_details_name', 'authorized_person_details_e-mail', 'authorized_person_details_mobile']].copy()
df_ap.columns = ['Name', 'Email', 'Phone no.']
df_ap['Profession'] = 'Authorized Person'
dfs_to_concat.append(df_ap)

# --- Extracting Promoters ---
df_promoter = df[['promoter_details_name', 'promoter_details_e-mail', 'promoter_details_mobile_number']].copy()
df_promoter.columns = ['Name', 'Email', 'Phone no.']
df_promoter['Profession'] = 'Promoter'
dfs_to_concat.append(df_promoter)

# --- Extracting Architects ---
df_architect = df[['project_architects_architect_name', 'project_architects_email']].copy()
df_architect.columns = ['Name', 'Email']
df_architect['Phone no.'] = np.nan # No phone number provided in source
df_architect['Profession'] = 'Architect'
dfs_to_concat.append(df_architect)

# --- Extracting Structural Engineers ---
df_se = df[['structural_engineers_engineer_name', 'structural_engineers_email']].copy()
df_se.columns = ['Name', 'Email']
df_se['Phone no.'] = np.nan # No phone number provided in source
df_se['Profession'] = 'Structural Engineer'
dfs_to_concat.append(df_se)

# 3. Combine all the extracted dataframes into one single sheet
final_df = pd.concat(dfs_to_concat, ignore_index=True)

# 4. Insert the Constant Source column
final_df.insert(0, 'Source', source_name)

# 5. Reorder columns as requested
final_df = final_df[['Source', 'Name', 'Profession', 'Email', 'Phone no.']]

# 6. Clean up: Drop rows where 'Name' is entirely missing
final_df.dropna(subset=['Name'], inplace=True)

# Clean up: Remove leading/trailing spaces from string columns
for col in ['Name', 'Profession', 'Email']:
    final_df[col] = final_df[col].astype(str).str.strip()

# Replace any textual 'nan' strings back to actual empty values
final_df.replace('nan', np.nan, inplace=True)

# 7. Save to a new Excel / CSV file
final_df.to_excel('../Cleaned_Goa_RERA_Contacts.xlsx', index=False)
# Use final_df.to_excel('Cleaned_Goa_RERA_Contacts.xlsx', index=False) if you specifically want an .xlsx file

print("Data cleaning complete. File saved!")
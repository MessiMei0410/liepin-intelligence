import React from 'react'

export const Metric = ({label,value,detail}: {label:string;value:React.ReactNode;detail:string}) => <div className="metric"><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>
export const SectionHead = ({title,meta}: {title:string;meta:string}) => <div className="section-head"><h2>{title}</h2><span>{meta}</span></div>
